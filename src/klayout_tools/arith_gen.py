"""Generate parallel-prefix adder RTL from an N x N binary **cell map**.

Pure library: :func:`run_arith_gen` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and writes files, but never prints --
serialisation and human-readable formatting live in the CLI command module
(``cli/arith_gen_cmd.py``), mirroring ``synthesize.py``/``equiv.py``.

**No dependency, no engine.** This module is pure Python string generation:
there is no Yosys/ABC/OpenROAD invocation anywhere in it. That is deliberate
-- the adder structures here are textbook, so the only thing worth measuring
is what *synthesis* does with them (see ``klt synthesize``'s ``arithmetic``
request field and ``docs/cli/synthesize.md``'s "Arithmetic architecture"
section), not the generator itself.

Background (issue #1722). Lai et al., *Scalable and Effective Arithmetic Tree
Generation for Adder and Multiplier Designs* (NeurIPS 2024,
`arXiv:2405.06758 <https://arxiv.org/abs/2405.06758>`_) represent an N-bit
prefix adder as an N x N binary matrix recording which prefix cells exist,
emit Verilog from that matrix, and score candidates by running them through a
real synthesis flow. Their *search* is RL/MCTS and their repo is unlicensed;
neither is reimplemented here. What is transferable -- and what this module
implements from scratch -- is the **representation** (the cell map) and the
**measured-in-loop selection** (``klt synthesize``'s side). At the bit widths
this repo's digital canaries use, the classical trees below need no search.

Representation
--------------

A prefix node ``(i, j)`` (``i >= j``) carries the group generate/propagate
pair over bits ``i`` down to ``j``::

    (G, P)_(i:j) = (g_i, p_i) o (g_{i-1}, p_{i-1}) o ... o (g_j, p_j)

under the associative prefix operator (the more-significant operand on the
left)::

    (g, p) o (g', p') = (g | (p & g'), p & p')

with ``g_i = a_i & b_i`` and ``p_i = a_i ^ b_i`` the bitwise
generate/propagate pair. The cell map is the N x N matrix ``M`` where
``M[i][j] == 1`` iff node ``(i, j)`` exists. The diagonal (``M[i][i]``, the
bitwise pairs) is always ``1``, the strict upper triangle is always ``0``,
and column ``0`` must be fully populated (``M[i][0] == 1`` for every ``i``)
-- those are the carries the adder actually needs.

A cell map is *structural*: it says which nodes exist, not how they are
wired. The wiring is recovered by the standard legalisation rule
(:func:`build_prefix_graph`): node ``(i, j)`` with ``i > j`` takes its upper
input from ``(i, k)``, where ``k`` is the **smallest** column ``> j`` that
exists in row ``i``, and its lower input from ``(k - 1, j)``. A map whose
``(k - 1, j)`` is missing is illegal and is rejected with a message naming
the offending node -- never silently "repaired".

Architectures
-------------

:data:`ARCHITECTURES` are emitted as cell maps by
:func:`cell_map_for_architecture`; all five are classical, and the
reconstruction rule above recovers exactly the textbook wiring for each:

``ripple``
    The serial prefix chain -- ``N - 1`` cells, ``N - 1`` levels. The
    smallest, slowest structure; the useful low-area end of the sweep.
``sklansky``
    Recursive doubling -- minimum depth (``ceil(log2 N)``), minimum cell
    count for that depth, but high fanout (up to ``N/2``).
``brent-kung``
    A reduce tree followed by an expand tree -- minimum cell count and
    fanout at the cost of roughly ``2 * log2 N`` depth.
``kogge-stone``
    Minimum depth with fanout capped at 2, paid for with the largest cell
    count (and the most wiring).
``han-carlson``
    Kogge-Stone over the odd bit positions, bracketed by one Brent-Kung-style
    stage at each end -- half Kogge-Stone's cells for one extra level.

Emitted artifacts
-----------------

:func:`run_arith_gen` writes four Verilog files plus a ready-to-run ``klt
equiv`` request next to each other:

``<module>.v``
    The structural prefix adder itself (``a``/``b``/``cin`` ->
    ``sum``/``cout``).
``<module>_ref.v``
    A behavioural reference with **identical ports** -- ``assign {cout, sum}
    = a + b + cin;`` -- so it can be the ``gold`` side of a ``klt equiv``
    proof against the structural module with no ``port_map``.
``<module>_tb.v``
    A self-checking testbench instantiating both, exhaustive for widths where
    that is cheap and pseudo-random otherwise.
``<module>_techmap.v``
    A Yosys ``techmap -map`` rule file defining ``\\$add`` in terms of the
    generated adder(s), guarded by ``_TECHMAP_FAIL_`` so only the widths
    actually generated are substituted. This is the file ``klt synthesize``'s
    ``arithmetic`` request field feeds into the synthesis script; it is
    emitted here too so the substitution is reproducible by hand.
``<module>_equiv_request.json``
    A ``klt equiv`` request pairing ``<module>_ref.v`` (gold) against
    ``<module>.v`` (gate), so acceptance is a single follow-up command.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

SCHEMA_VERSION = 1

#: The named prefix-adder architectures :func:`cell_map_for_architecture`
#: knows how to build, in the order they are swept by ``klt synthesize``'s
#: ``arithmetic.adders: "auto"`` (cheapest structure first).
ARCHITECTURES: tuple[str, ...] = (
    "ripple",
    "brent-kung",
    "han-carlson",
    "sklansky",
    "kogge-stone",
)

#: Widths at or below this bound get an exhaustive testbench; wider ones get
#: a pseudo-random one. ``2 * 8 + 1 = 17`` bits of stimulus is ~131k vectors,
#: which Icarus runs in well under a second.
_EXHAUSTIVE_TB_WIDTH = 8

#: Vector count for the pseudo-random testbench used above
#: :data:`_EXHAUSTIVE_TB_WIDTH`.
_RANDOM_TB_VECTORS = 20000

MAX_WIDTH = 1024


class ArithGenError(Exception):
    """Raised when a prefix adder cannot be generated: an out-of-range
    width, an unknown architecture name, a malformed or illegal cell map, or
    an unwritable output directory.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- either the Verilog is written, or this is raised.
    """


def normalize_architecture(architecture: str) -> str:
    """Canonicalise an architecture name.

    Accepts the underscore spelling as well as the hyphenated one
    (``brent_kung`` == ``brent-kung``) and is case-insensitive, so a caller
    can pass either the CLI spelling or the Verilog-identifier spelling.
    Raises :class:`ArithGenError` naming the supported set for anything else.
    """
    if not isinstance(architecture, str) or not architecture:
        raise ArithGenError(
            "architecture must be a non-empty string "
            f"(supported: {', '.join(ARCHITECTURES)})"
        )
    candidate = architecture.strip().lower().replace("_", "-")
    if candidate not in ARCHITECTURES:
        raise ArithGenError(
            f"unknown adder architecture '{architecture}' "
            f"(supported: {', '.join(ARCHITECTURES)})"
        )
    return candidate


def _validate_width(width: Any) -> int:
    if isinstance(width, bool) or not isinstance(width, int):
        raise ArithGenError("width must be an integer")
    if width < 1:
        raise ArithGenError(f"width must be >= 1 (got {width})")
    if width > MAX_WIDTH:
        raise ArithGenError(f"width must be <= {MAX_WIDTH} (got {width})")
    return width


# --------------------------------------------------------------------------
# Cell maps
# --------------------------------------------------------------------------


def cell_map_for_architecture(architecture: str, width: int) -> list[list[int]]:
    """Build the N x N cell map for a named ``architecture`` at ``width``.

    The returned matrix is row-major with ``M[i][j] == 1`` iff prefix node
    ``(i, j)`` exists; the diagonal is always set and the strict upper
    triangle is always clear. Every architecture below populates column ``0``
    completely, so the map is always a legal adder (asserted by
    :func:`build_prefix_graph`, which every caller runs next).

    Widths that are not powers of two are handled by clamping each node's
    lower bound at ``0`` and skipping nodes whose upper bound would exceed
    ``width - 1`` -- the standard truncation, not a separate code path.
    """
    architecture = normalize_architecture(architecture)
    width = _validate_width(width)

    nodes: set[tuple[int, int]] = {(i, i) for i in range(width)}

    if architecture == "ripple":
        _add_ripple(nodes, width)
    elif architecture == "sklansky":
        _add_sklansky(nodes, width)
    elif architecture == "brent-kung":
        _add_brent_kung(nodes, width)
    elif architecture == "kogge-stone":
        _add_kogge_stone(nodes, width)
    elif architecture == "han-carlson":
        _add_han_carlson(nodes, width)
    else:  # pragma: no cover - normalize_architecture already gated this
        raise ArithGenError(f"unknown adder architecture '{architecture}'")

    return nodes_to_cell_map(nodes, width)


def nodes_to_cell_map(nodes: set[tuple[int, int]], width: int) -> list[list[int]]:
    """Render a ``{(i, j)}`` node set as the N x N 0/1 matrix."""
    cell_map = [[0] * width for _ in range(width)]
    for i, j in nodes:
        cell_map[i][j] = 1
    return cell_map


def _add_ripple(nodes: set[tuple[int, int]], width: int) -> None:
    """Serial prefix chain: ``(i, 0) = (i, i) o (i - 1, 0)``."""
    for i in range(1, width):
        nodes.add((i, 0))


def _add_sklansky(nodes: set[tuple[int, int]], width: int) -> None:
    """Recursive doubling (Sklansky).

    At level ``l`` the design is partitioned into groups of ``2 ** l``
    consecutive bits; every bit in a group's **upper** half takes the group's
    lower-half prefix as its second input, producing node ``(i, base)``.
    """
    level = 1
    while (1 << (level - 1)) < width:
        span = 1 << level
        half = span >> 1
        base = 0
        while base < width:
            for i in range(base + half, min(base + span, width)):
                nodes.add((i, base))
            base += span
        level += 1


def _add_brent_kung(nodes: set[tuple[int, int]], width: int) -> None:
    """Brent-Kung: a reduce (forward) tree followed by an expand (backward)
    tree.

    The forward tree halves the live positions at each level until only bit
    ``2 ** L - 1`` remains; the backward tree fills in the positions the
    forward tree skipped, from the coarsest stride down to stride 1.
    """
    # Forward (reduce) tree: at level l, stride 2**l, node (i, i - 2**l + 1).
    level = 1
    top_level = 0
    while (1 << level) - 1 < width:
        span = 1 << level
        for i in range(span - 1, width, span):
            nodes.add((i, i - span + 1))
        top_level = level
        level += 1

    # Backward (expand) tree: fill in the positions the forward tree skipped,
    # coarsest stride first. Every node it adds closes onto column 0 -- its
    # lower input is the already-complete ``(i - 2**(l-1), 0)`` node.
    for level in range(top_level, 0, -1):
        span = 1 << level
        half = span >> 1
        for i in range(span + half - 1, width, span):
            nodes.add((i, 0))


def _add_kogge_stone(nodes: set[tuple[int, int]], width: int) -> None:
    """Kogge-Stone: at level ``l`` every bit ``i >= 2 ** (l - 1)`` combines
    its current prefix with the one ``2 ** (l - 1)`` positions below."""
    level = 1
    while (1 << (level - 1)) < width:
        stride = 1 << (level - 1)
        for i in range(stride, width):
            nodes.add((i, max(0, i - (stride << 1) + 1)))
        level += 1


def _add_han_carlson(nodes: set[tuple[int, int]], width: int) -> None:
    """Han-Carlson: one Brent-Kung stage, Kogge-Stone over the odd bit
    positions, then one final stage recovering the even positions."""
    # Stage 1 -- pair up neighbours so every odd position holds (i : i-1).
    for i in range(1, width, 2):
        nodes.add((i, i - 1))

    # Kogge-Stone restricted to the odd positions.
    level = 2
    while (1 << (level - 2)) < width:
        stride = 1 << (level - 1)
        for i in range(1, width, 2):
            if i >= stride:
                nodes.add((i, max(0, i - (stride << 1) + 1)))
        level += 1

    # Final stage -- every even position picks up the odd prefix below it.
    for i in range(2, width, 2):
        nodes.add((i, 0))


# --------------------------------------------------------------------------
# Cell map -> prefix graph
# --------------------------------------------------------------------------


class PrefixNode:
    """One node of a legalised prefix graph.

    ``(msb, lsb)`` is the bit range this node's ``(G, P)`` pair covers;
    ``upper``/``lower`` are the two input nodes (both ``None`` for a diagonal
    node, which is just the bitwise ``(g_i, p_i)`` pair); ``level`` is the
    node's logic depth in prefix cells (``0`` for a diagonal node).
    """

    __slots__ = ("msb", "lsb", "upper", "lower", "level")

    def __init__(
        self,
        msb: int,
        lsb: int,
        upper: tuple[int, int] | None,
        lower: tuple[int, int] | None,
        level: int,
    ) -> None:
        self.msb = msb
        self.lsb = lsb
        self.upper = upper
        self.lower = lower
        self.level = level

    @property
    def key(self) -> tuple[int, int]:
        return (self.msb, self.lsb)

    @property
    def is_bitwise(self) -> bool:
        return self.msb == self.lsb

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PrefixNode({self.msb}, {self.lsb}, level={self.level})"


class PrefixGraph:
    """A validated, topologically-ordered prefix graph plus its metrics."""

    def __init__(self, width: int, nodes: list[PrefixNode]) -> None:
        self.width = width
        self.nodes = nodes
        self.by_key = {node.key: node for node in nodes}

    @property
    def prefix_cells(self) -> int:
        """Number of real prefix cells (the diagonal is free wiring)."""
        return sum(1 for node in self.nodes if not node.is_bitwise)

    @property
    def logic_levels(self) -> int:
        """Depth, in prefix cells, of the deepest carry this adder needs."""
        return max(self.by_key[(i, 0)].level for i in range(self.width))

    @property
    def max_fanout(self) -> int:
        """Largest number of prefix cells driven by any single node -- the
        structural proxy for the loading a real cell library will have to
        buffer (Sklansky's weakness, Kogge-Stone's advantage)."""
        counts: dict[tuple[int, int], int] = {}
        for node in self.nodes:
            for parent in (node.upper, node.lower):
                if parent is not None:
                    counts[parent] = counts.get(parent, 0) + 1
        return max(counts.values()) if counts else 0

    def metrics(self) -> dict[str, int]:
        return {
            "prefix_cells": self.prefix_cells,
            "logic_levels": self.logic_levels,
            "max_fanout": self.max_fanout,
        }


def validate_cell_map(cell_map: Any) -> list[list[int]]:
    """Validate an externally-supplied cell map and return it normalised to
    a list-of-lists of ``0``/``1`` ints.

    Rejects (naming the offending entry): a non-square/ragged matrix, a
    non-0/1 entry, a clear diagonal entry, a set entry in the strict upper
    triangle, and an incomplete column ``0``. These are *structural*
    requirements -- wiring legality is checked separately by
    :func:`build_prefix_graph`.
    """
    if not isinstance(cell_map, list) or not cell_map:
        raise ArithGenError("cell_map must be a non-empty list of rows")
    width = len(cell_map)
    if width > MAX_WIDTH:
        raise ArithGenError(f"cell_map width must be <= {MAX_WIDTH} (got {width})")
    normalized: list[list[int]] = []
    for i, row in enumerate(cell_map):
        if not isinstance(row, list) or len(row) != width:
            raise ArithGenError(
                f"cell_map row {i} must be a list of exactly {width} entries "
                "(the matrix must be square)"
            )
        out_row: list[int] = []
        for j, entry in enumerate(row):
            if isinstance(entry, bool):
                entry = int(entry)
            if entry not in (0, 1):
                raise ArithGenError(
                    f"cell_map[{i}][{j}] must be 0 or 1 (got {entry!r})"
                )
            if j > i and entry:
                raise ArithGenError(
                    f"cell_map[{i}][{j}] is set above the diagonal -- a prefix "
                    "node (i, j) requires i >= j"
                )
            out_row.append(int(entry))
        if out_row[i] != 1:
            raise ArithGenError(
                f"cell_map[{i}][{i}] must be 1 -- every bit position's own "
                "(g, p) pair always exists"
            )
        normalized.append(out_row)
    for i in range(width):
        if normalized[i][0] != 1:
            raise ArithGenError(
                f"cell_map[{i}][0] must be 1 -- the adder needs the carry out "
                f"of bit {i}, i.e. the prefix node ({i}, 0)"
            )
    return normalized


def build_prefix_graph(cell_map: Any) -> PrefixGraph:
    """Legalise a cell map into a wired, topologically-ordered
    :class:`PrefixGraph`.

    Wiring rule (the standard one, see this module's docstring): node
    ``(i, j)`` with ``i > j`` takes its upper input from ``(i, k)`` where
    ``k`` is the smallest column strictly greater than ``j`` that is set in
    row ``i`` (the diagonal guarantees one exists), and its lower input from
    ``(k - 1, j)``. A missing lower input makes the map **illegal** and
    raises :class:`ArithGenError` naming both nodes -- this module never
    silently inserts the missing cell, because doing so would mean the
    emitted Verilog no longer matches the cell map the caller handed in.

    Returns nodes ordered by ``(level, msb, lsb)``, which is both a valid
    topological order (a node's level is strictly greater than either
    input's) and a stable emission order, so the generated Verilog is
    byte-identical across runs.
    """
    normalized = validate_cell_map(cell_map)
    width = len(normalized)

    wiring: dict[tuple[int, int], tuple[tuple[int, int], tuple[int, int]] | None] = {}
    for i in range(width):
        for j in range(i + 1):
            if not normalized[i][j]:
                continue
            if i == j:
                wiring[(i, j)] = None
                continue
            upper_k = next(k for k in range(j + 1, i + 1) if normalized[i][k])
            lower = (upper_k - 1, j)
            if not normalized[lower[0]][lower[1]]:
                raise ArithGenError(
                    f"illegal cell map: prefix node ({i}, {j}) needs its lower "
                    f"input ({lower[0]}, {lower[1]}), which the map does not "
                    "contain"
                )
            wiring[(i, j)] = ((i, upper_k), lower)

    levels: dict[tuple[int, int], int] = {}

    def _level(key: tuple[int, int]) -> int:
        cached = levels.get(key)
        if cached is not None:
            return cached
        parents = wiring[key]
        value = (
            0 if parents is None else 1 + max(_level(parents[0]), _level(parents[1]))
        )
        levels[key] = value
        return value

    nodes = [
        PrefixNode(
            msb=key[0],
            lsb=key[1],
            upper=None if wiring[key] is None else wiring[key][0],
            lower=None if wiring[key] is None else wiring[key][1],
            level=_level(key),
        )
        for key in wiring
    ]
    nodes.sort(key=lambda node: (node.level, node.msb, node.lsb))
    return PrefixGraph(width, nodes)


# --------------------------------------------------------------------------
# Verilog emission
# --------------------------------------------------------------------------

_HEADER = (
    "// Generated by `klt arith-gen` (klayout_tools.arith_gen) -- do not edit.\n"
    "// See docs/cli/arith-gen.md. Regenerate rather than patching by hand.\n"
)


def module_identifier(name: str) -> str:
    """Validate a Verilog module identifier (simple identifiers only)."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        raise ArithGenError(
            f"module name '{name}' is not a valid Verilog identifier "
            "(letters, digits, '_' and '$', not starting with a digit)"
        )
    return name


def default_module_name(architecture: str, width: int) -> str:
    """``klt_add_<architecture>_<width>`` with hyphens folded to
    underscores, e.g. ``klt_add_kogge_stone_16``."""
    return f"klt_add_{normalize_architecture(architecture).replace('-', '_')}_{width}"


def _node_wire(node_key: tuple[int, int], signal: str) -> str:
    msb, lsb = node_key
    if msb == lsb:
        return f"{signal}0[{msb}]"
    return f"{signal}_{msb}_{lsb}"


def emit_adder_verilog(
    graph: PrefixGraph,
    module_name: str,
    *,
    architecture: str | None = None,
) -> str:
    """Emit the structural prefix adder for ``graph`` as Verilog-2001 text.

    Ports are ``a``/``b``/``cin`` -> ``sum``/``cout``, with
    ``{cout, sum} == a + b + cin``. The carry into bit ``i`` is
    ``G_(i-1:0) | (P_(i-1:0) & cin)`` -- folding ``cin`` in after the prefix
    network rather than as an extra prefix column, so the emitted network is
    exactly the cell map that was handed in.
    """
    module_identifier(module_name)
    width = graph.width
    hi = width - 1
    metrics = graph.metrics()

    lines: list[str] = [_HEADER.rstrip("\n")]
    if architecture is not None:
        lines.append(f"// architecture: {architecture}")
    lines.append(f"// width: {width} bits")
    lines.append(
        f"// prefix cells: {metrics['prefix_cells']}, "
        f"logic levels: {metrics['logic_levels']}, "
        f"max prefix fanout: {metrics['max_fanout']}"
    )
    lines.append("")
    lines.append(f"module {module_name} (a, b, cin, sum, cout);")
    lines.append(f"  input  [{hi}:0] a;")
    lines.append(f"  input  [{hi}:0] b;")
    lines.append("  input         cin;")
    lines.append(f"  output [{hi}:0] sum;")
    lines.append("  output        cout;")
    lines.append("")
    lines.append(f"  wire [{hi}:0] g0;")
    lines.append(f"  wire [{hi}:0] p0;")
    lines.append("  assign g0 = a & b;")
    lines.append("  assign p0 = a ^ b;")
    lines.append("")

    cells = [node for node in graph.nodes if not node.is_bitwise]
    if cells:
        lines.append("  // Prefix network, in topological (level) order.")
        current_level = None
        for node in cells:
            if node.level != current_level:
                current_level = node.level
                lines.append(f"  // level {current_level}")
            gname = _node_wire(node.key, "g")
            pname = _node_wire(node.key, "p")
            assert node.upper is not None and node.lower is not None
            gu = _node_wire(node.upper, "g")
            pu = _node_wire(node.upper, "p")
            gl = _node_wire(node.lower, "g")
            pl = _node_wire(node.lower, "p")
            lines.append(f"  wire {gname}, {pname};")
            lines.append(f"  assign {gname} = {gu} | ({pu} & {gl});")
            lines.append(f"  assign {pname} = {pu} & {pl};")
        lines.append("")

    lines.append("  // Carries: c[i] is the carry into bit i; c[0] is cin.")
    lines.append(f"  wire [{width}:0] c;")
    lines.append("  assign c[0] = cin;")
    for i in range(width):
        gname = _node_wire((i, 0), "g")
        pname = _node_wire((i, 0), "p")
        lines.append(f"  assign c[{i + 1}] = {gname} | ({pname} & cin);")
    lines.append("")
    lines.append(f"  assign sum = p0 ^ c[{hi}:0];")
    lines.append(f"  assign cout = c[{width}];")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def emit_reference_verilog(module_name: str, width: int) -> str:
    """Emit the behavioural ``a + b + cin`` reference with the same ports as
    :func:`emit_adder_verilog`'s output -- the ``gold`` side of the ``klt
    equiv`` proof (no ``port_map`` needed)."""
    module_identifier(module_name)
    width = _validate_width(width)
    hi = width - 1
    return (
        _HEADER
        + "// Behavioural reference: the `+` this adder must be equivalent to.\n"
        f"\nmodule {module_name} (a, b, cin, sum, cout);\n"
        f"  input  [{hi}:0] a;\n"
        f"  input  [{hi}:0] b;\n"
        "  input         cin;\n"
        f"  output [{hi}:0] sum;\n"
        "  output        cout;\n"
        "\n"
        "  assign {cout, sum} = a + b + cin;\n"
        "endmodule\n"
    )


def emit_testbench_verilog(
    module_name: str,
    reference_name: str,
    width: int,
    *,
    tb_name: str | None = None,
) -> str:
    """Emit a self-checking testbench driving the structural adder and the
    behavioural reference with identical stimulus and comparing both outputs.

    Exhaustive over all ``2 ** (2 * width + 1)`` input combinations when
    ``width <= 8``; otherwise :data:`_RANDOM_TB_VECTORS` pseudo-random
    vectors plus the corner cases (all-zero, all-one, and the carry-chain
    worst case ``a = all ones, b = 1``). Plain Verilog-2001 with ``$random``
    -- runnable by Icarus (``iverilog``) with no plusargs or VPI.
    """
    module_identifier(module_name)
    module_identifier(reference_name)
    width = _validate_width(width)
    tb_name = module_identifier(tb_name or f"{module_name}_tb")
    hi = width - 1

    lines = [
        _HEADER.rstrip("\n"),
        "// Self-checking testbench: structural adder vs. behavioural `+`.",
        "",
        f"module {tb_name};",
        f"  reg  [{hi}:0] a;",
        f"  reg  [{hi}:0] b;",
        "  reg          cin;",
        f"  wire [{hi}:0] sum_dut;",
        "  wire         cout_dut;",
        f"  wire [{hi}:0] sum_ref;",
        "  wire         cout_ref;",
        "  integer errors;",
        "  integer vectors;",
        "",
        f"  {module_name} dut (",
        "      .a(a), .b(b), .cin(cin), .sum(sum_dut), .cout(cout_dut));",
        f"  {reference_name} golden (",
        "      .a(a), .b(b), .cin(cin), .sum(sum_ref), .cout(cout_ref));",
        "",
        "  task check;",
        "    begin",
        "      #1;",
        "      vectors = vectors + 1;",
        "      if (sum_dut !== sum_ref || cout_dut !== cout_ref) begin",
        "        errors = errors + 1;",
        "        if (errors <= 10)",
        '          $display("MISMATCH a=%h b=%h cin=%b dut=%b%h ref=%b%h",',
        "                   a, b, cin, cout_dut, sum_dut, cout_ref, sum_ref);",
        "      end",
        "    end",
        "  endtask",
        "",
        "  initial begin",
        "    errors = 0;",
        "    vectors = 0;",
    ]

    if width <= _EXHAUSTIVE_TB_WIDTH:
        lines += [
            "    begin : exhaustive",
            "      integer ia;",
            "      integer ib;",
            "      integer ic;",
            f"      for (ia = 0; ia < {1 << width}; ia = ia + 1)",
            f"        for (ib = 0; ib < {1 << width}; ib = ib + 1)",
            "          for (ic = 0; ic < 2; ic = ic + 1) begin",
            "            a = ia;",
            "            b = ib;",
            "            cin = ic;",
            "            check;",
            "          end",
            "    end",
        ]
    else:
        ones = f"{{{width}{{1'b1}}}}"
        lines += [
            "    // Corner cases first, then pseudo-random vectors.",
            "    a = 0; b = 0; cin = 0; check;",
            "    a = 0; b = 0; cin = 1; check;",
            f"    a = {ones}; b = {ones}; cin = 1; check;",
            f"    a = {ones}; b = 1; cin = 0; check;",
            f"    a = {ones}; b = 0; cin = 1; check;",
            "    begin : randomised",
            "      integer n;",
            f"      for (n = 0; n < {_RANDOM_TB_VECTORS}; n = n + 1) begin",
            "        a = $random;",
            "        b = $random;",
            "        cin = $random;",
            "        check;",
            "      end",
            "    end",
        ]

    lines += [
        "    if (errors == 0)",
        '      $display("PASS: %0d vectors, 0 mismatches", vectors);',
        "    else",
        '      $display("FAIL: %0d vectors, %0d mismatches", vectors, errors);',
        "    $finish;",
        "  end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def emit_techmap_verilog(entries: dict[int, str]) -> str:
    """Emit a Yosys ``techmap -map`` rule file substituting the generated
    adders for Yosys's own ``$add`` expansion.

    ``entries`` maps bit width -> generated adder module name. The emitted
    ``\\$add`` module:

    - refuses (``_TECHMAP_FAIL_``) any ``$add`` whose ``Y_WIDTH`` is not one
      of the generated widths, leaving Yosys's default expansion in place for
      every other adder in the design -- so this substitution is always a
      *subset* replacement, never an all-or-nothing one;
    - sign- or zero-extends ``A``/``B`` to ``Y_WIDTH`` per the cell's own
      ``A_SIGNED``/``B_SIGNED`` parameters (truncating instead when an
      operand is wider than the result, which is what ``$add``'s own
      semantics require), so a signed ``+`` is substituted just as correctly
      as an unsigned one;
    - ties ``cin`` low and drops ``cout`` -- ``$add`` has no carry-in/out
      ports, and Yosys's ``opt``/ABC remove the dead carry-out cone.

    The widths are emitted in a ``generate ... if`` chain on the ``Y_WIDTH``
    parameter, which Yosys elaborates per-instance because ``techmap``
    specialises the rule module for each distinct parameter set.
    """
    if not entries:
        raise ArithGenError("techmap rule file needs at least one adder width")
    widths = sorted(entries)
    for width in widths:
        _validate_width(width)
        module_identifier(entries[width])

    fail_terms = " && ".join(f"(Y_WIDTH != {width})" for width in widths)
    lines = [
        _HEADER.rstrip("\n"),
        "// Yosys `techmap -map` rules: substitute the generated prefix",
        "// adder(s) for $add cells of the generated width(s).",
        "",
        "module \\$add (A, B, Y);",
        "  parameter A_SIGNED = 0;",
        "  parameter B_SIGNED = 0;",
        "  parameter A_WIDTH = 1;",
        "  parameter B_WIDTH = 1;",
        "  parameter Y_WIDTH = 1;",
        "",
        "  input  [A_WIDTH-1:0] A;",
        "  input  [B_WIDTH-1:0] B;",
        "  output [Y_WIDTH-1:0] Y;",
        "",
        f"  wire _TECHMAP_FAIL_ = {fail_terms};",
        "",
        "  wire [Y_WIDTH-1:0] AA;",
        "  wire [Y_WIDTH-1:0] BB;",
        "  wire [Y_WIDTH-1:0] YY;",
        "",
        "  generate",
        "    if (A_WIDTH >= Y_WIDTH)",
        "      assign AA = A[Y_WIDTH-1:0];",
        "    else if (A_SIGNED)",
        "      assign AA = {{(Y_WIDTH-A_WIDTH){A[A_WIDTH-1]}}, A};",
        "    else",
        "      assign AA = {{(Y_WIDTH-A_WIDTH){1'b0}}, A};",
        "",
        "    if (B_WIDTH >= Y_WIDTH)",
        "      assign BB = B[Y_WIDTH-1:0];",
        "    else if (B_SIGNED)",
        "      assign BB = {{(Y_WIDTH-B_WIDTH){B[B_WIDTH-1]}}, B};",
        "    else",
        "      assign BB = {{(Y_WIDTH-B_WIDTH){1'b0}}, B};",
        "  endgenerate",
        "",
        "  generate",
    ]
    for index, width in enumerate(widths):
        keyword = "if" if index == 0 else "else if"
        lines.append(f"    {keyword} (Y_WIDTH == {width})")
        lines.append(
            f"      {entries[width]} _klt_add_impl ("
            ".a(AA), .b(BB), .cin(1'b0), .sum(YY), .cout());"
        )
    lines += [
        "  endgenerate",
        "",
        "  assign Y = YY;",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------


def generate_adder(
    *,
    width: int,
    architecture: str | None = None,
    cell_map: Any = None,
    module_name: str | None = None,
) -> dict[str, Any]:
    """Build the graph + Verilog text for one adder, without writing files.

    Exactly one of ``architecture`` / ``cell_map`` selects the structure: a
    named architecture builds its own cell map, an explicit cell map is used
    as given (and ``width`` must match its dimension). Returns a dict with
    the resolved ``architecture`` (``"custom"`` for an explicit map),
    ``cell_map``, ``graph``, metrics, and the four Verilog texts.
    """
    if (architecture is None) == (cell_map is None):
        raise ArithGenError(
            "exactly one of architecture or cell_map must be given "
            "(a named architecture builds its own cell map)"
        )

    if cell_map is not None:
        resolved_map = validate_cell_map(cell_map)
        if width is not None and width != len(resolved_map):
            raise ArithGenError(
                f"width {width} does not match the {len(resolved_map)}x"
                f"{len(resolved_map)} cell map"
            )
        width = len(resolved_map)
        resolved_arch = "custom"
    else:
        width = _validate_width(width)
        resolved_arch = normalize_architecture(str(architecture))
        resolved_map = cell_map_for_architecture(resolved_arch, width)

    graph = build_prefix_graph(resolved_map)

    if module_name is None:
        module_name = (
            f"klt_add_custom_{width}"
            if resolved_arch == "custom"
            else default_module_name(resolved_arch, width)
        )
    module_identifier(module_name)
    reference_name = f"{module_name}_ref"

    return {
        "architecture": resolved_arch,
        "width": width,
        "module_name": module_name,
        "reference_name": reference_name,
        "cell_map": resolved_map,
        "graph": graph,
        "metrics": graph.metrics(),
        "verilog": emit_adder_verilog(graph, module_name, architecture=resolved_arch),
        "reference_verilog": emit_reference_verilog(reference_name, width),
        "testbench_verilog": emit_testbench_verilog(module_name, reference_name, width),
        "techmap_verilog": emit_techmap_verilog({width: module_name}),
    }


def run_arith_gen(
    *,
    width: int | None = None,
    architecture: str | None = None,
    cell_map_path: str | None = None,
    module_name: str | None = None,
    output_dir: str | None = None,
    include_cell_map: bool = False,
) -> dict[str, Any]:
    """Generate a prefix adder and write its artifacts to ``output_dir``.

    ``cell_map_path`` names a JSON file holding either a bare N x N matrix or
    an object with a ``cell_map`` key (the shape this command's own
    ``--format json`` payload emits with ``--include-cell-map``, so a payload
    can be round-tripped straight back in).

    Returns the documented ``klt arith-gen`` payload (see
    ``docs/cli/arith-gen.md``). Raises :class:`ArithGenError` for a bad
    width/architecture/cell map or an unwritable output directory.
    """
    cell_map: Any = None
    if cell_map_path is not None:
        if architecture is not None:
            raise ArithGenError(
                "--arch and --cell-map are mutually exclusive -- a cell map "
                "already fixes the structure"
            )
        cell_map = _load_cell_map(cell_map_path)

    if cell_map is None and architecture is None:
        raise ArithGenError("one of --arch or --cell-map is required")
    if cell_map is None and width is None:
        raise ArithGenError("--width is required with --arch")

    built = generate_adder(
        width=width if width is not None else len(cell_map),
        architecture=architecture,
        cell_map=cell_map,
        module_name=module_name,
    )

    resolved_dir = os.path.abspath(output_dir or os.getcwd())
    try:
        os.makedirs(resolved_dir, exist_ok=True)
    except OSError as exc:
        raise ArithGenError(
            f"could not create output directory '{resolved_dir}': {exc}"
        ) from exc

    module = built["module_name"]
    paths = {
        "verilog_path": os.path.join(resolved_dir, f"{module}.v"),
        "reference_path": os.path.join(resolved_dir, f"{module}_ref.v"),
        "testbench_path": os.path.join(resolved_dir, f"{module}_tb.v"),
        "techmap_path": os.path.join(resolved_dir, f"{module}_techmap.v"),
    }
    _write_text(paths["verilog_path"], built["verilog"])
    _write_text(paths["reference_path"], built["reference_verilog"])
    _write_text(paths["testbench_path"], built["testbench_verilog"])
    _write_text(paths["techmap_path"], built["techmap_verilog"])

    equiv_request_path = os.path.join(resolved_dir, f"{module}_equiv_request.json")
    equiv_request = {
        "schema": "klt.equiv.request/1",
        "engine": "yosys",
        "gold": {"sources": [paths["reference_path"]], "top": built["reference_name"]},
        "gate": {"sources": [paths["verilog_path"]], "top": module},
    }
    _write_text(equiv_request_path, json.dumps(equiv_request, indent=2) + "\n")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "architecture": built["architecture"],
        "width": built["width"],
        "module_name": module,
        "reference_module_name": built["reference_name"],
        "prefix_cells": built["metrics"]["prefix_cells"],
        "logic_levels": built["metrics"]["logic_levels"],
        "max_fanout": built["metrics"]["max_fanout"],
        "output_dir": resolved_dir,
        "equiv_request_path": equiv_request_path,
        "cell_map": built["cell_map"] if include_cell_map else None,
    }
    payload.update(paths)
    return payload


def _load_cell_map(path: str) -> list[list[int]]:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise ArithGenError(f"cell map file not found: {path}") from exc
    except OSError as exc:
        raise ArithGenError(f"could not read cell map file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArithGenError(f"cell map file '{path}' is not valid JSON: {exc}") from exc

    if isinstance(document, dict):
        document = document.get("cell_map")
        if document is None:
            raise ArithGenError(
                f"cell map file '{path}' is a JSON object with no 'cell_map' key"
            )
    return validate_cell_map(document)


def _write_text(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise ArithGenError(f"could not write '{path}': {exc}") from exc
