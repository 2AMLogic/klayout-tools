"""SDF (Standard Delay Format) timing-annotation subsystem for the cocotb
functional-verification flow.

Split out of ``functional_verification.py`` (issue #1909) as a
self-contained, low-coupling subsystem: option validation
(:func:`_reject_sdf_with_functional_models`, :func:`_resolve_sdf_option`),
elaboration-root generation (:func:`_write_sdf_annotate_shim`,
:func:`_parse_toplevel_ports`, :func:`_write_sdf_dut_wrapper`), SDF-text
paren/clause scanning and bit-selected top-level-port ``INTERCONNECT``
deferral (:func:`_sdf_matching_paren`, :func:`_sdf_top_level_clauses`,
:func:`_split_sdf_bus_port_interconnects`), ``assign``-aliased-port
``INTERCONNECT`` normalization (:func:`_collect_sdf_alias_port_bits`,
:func:`_sdf_interconnect_is_zero_delay`,
:func:`_sdf_interconnect_is_droppable`, :func:`_sdf_cell_delay_spans`,
:func:`_sdf_cell_entry_removals`, :func:`_cut_sdf_spans`,
:func:`_read_sdf_text`, :func:`_drop_sdf_interconnect_entries`,
:func:`_drop_sdf_non_interconnect_entries`,
:func:`_drop_sdf_zero_delay_alias_port_interconnects`), physical-only-
destination ``INTERCONNECT`` normalization
(:func:`_sdf_cell_model_is_physical_only`,
:func:`_collect_sdf_physical_only_instances`,
:func:`_sdf_interconnect_is_physical_only_droppable`,
:func:`_drop_sdf_zero_delay_physical_only_interconnects`), header-triple
normalization (:func:`_normalize_sdf_header_triples`), and post-run diagnostics
(:func:`_check_sdf_engine_capability`, :func:`_scan_sdf_diagnostics`). This
mirrors the shape of the earlier ``lvs.py``/``lvs_mismatch.py`` (#1721),
``extract.py``/``extract_parasitics.py`` (#1572), and
``extract.py``/``extract_spef.py`` (#1195/#1200) splits: a cohesive,
low-coupling subsystem relocated out of a file that had grown past the
point one module should hold request/response orchestration, mutation
testing, *and* SDF handling.

Request/response orchestration (``run_functional_verification``,
``run_functional_verification_with_mutations``) and the mutation-testing
subsystem are *not* part of this split -- they stay in
``functional_verification.py``, calling into this module's entry points
exactly as they called the same functions when this was one file.

Dependency surface is intentionally narrow: this module's only outbound
coupling to ``functional_verification.py`` is
:class:`~klayout_tools.functional_verification.FunctionalVerificationError`
plus a handful of ``SDF_*`` engine/diagnostic constants, imported *inside*
the handful of functions that use them (the same deferred-import discipline
``lvs_mismatch.py`` uses to depend on ``lvs.py``) rather than at module
scope, so this module never has a load-time dependency back on
``functional_verification.py`` -- only ``functional_verification.py``
depends on this module at import time. ``functional_verification.py`` in
turn imports this module's entry points back (module scope, no cycle --
this module never imports ``functional_verification`` at its own module
scope) to preserve ``klayout_tools.functional_verification.<name>`` as a
working import path for every name the test suite/callers used before this
split.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from .functional_verification_sdf_rename import _sdf_unresolvable_pin_tokens


def _reject_sdf_with_functional_models(
    sdf: dict[str, str] | None, defines: dict[str, str | None]
) -> None:
    """``options.sdf`` together with a ``FUNCTIONAL`` define is
    self-contradictory, and is rejected rather than run (issue #1004's second
    finding, folded into #1002's own implementation).

    A PDK's behavioural cell models put their zero-delay models and their
    SDF-annotatable *timing* models in the two branches of the same
    `` `ifdef FUNCTIONAL `` guard, and only the non-``FUNCTIONAL`` branch
    carries the ``specify`` blocks an SDF's ``IOPATH`` entries annotate. So a
    request that asks for real post-route delays *and* selects the zero-delay
    models is asking for two incompatible things: at best the annotation
    matches nothing, at worst (Icarus 12.0, observed in #1004) the run
    silently mis-simulates and every flop samples ``x`` with no error raised.

    Either failure lands as a *quietly wrong* verdict, which is the exact
    class this feature's transcript gate exists to prevent -- so it is
    checked at request-validation time, the same posture
    ``options.coverage`` + ``engine: "icarus"`` already takes.
    """
    from .functional_verification import FunctionalVerificationError

    if sdf is None or "FUNCTIONAL" not in defines:
        return
    raise FunctionalVerificationError(
        "options.sdf cannot be combined with a 'FUNCTIONAL' define -- a PDK's "
        "FUNCTIONAL cell models are the zero-delay branch of the same `ifdef "
        "that guards the timing models, so they carry none of the specify "
        "blocks an SDF's IOPATH entries annotate. Drop the FUNCTIONAL define "
        "to re-simulate with real delays, or drop options.sdf to keep the "
        "zero-delay run"
    )


def _resolve_sdf_option(
    sdf: Any, engine: str, request_dir: str
) -> dict[str, str] | None:
    """Validate the optional ``request.options.sdf`` block (issue #1002) and
    return ``{"file": <abs path>, "corner": ..., "entries": ...}``, or
    ``None`` when absent.

    Unknown keys are rejected rather than ignored. The block's whole surface
    is three fields, and each of the plausible typos (``"corners"``,
    ``"path"``, ``"entry"``) would otherwise degrade to a *silently different
    run* -- the wrong timing corner, no annotation at all, or every delay
    annotated when only net delays were meant -- which is precisely the class
    of failure this feature's own transcript gate exists to prevent.
    """
    from .functional_verification import (
        DEFAULT_SDF_CORNER,
        DEFAULT_SDF_ENTRIES,
        SDF_CORNERS,
        SDF_ENGINES,
        SDF_ENTRIES_MODES,
        FunctionalVerificationError,
    )

    if sdf is None:
        return None
    if not isinstance(sdf, dict):
        raise FunctionalVerificationError("request.options.sdf must be a JSON object")
    if engine not in SDF_ENGINES:
        raise FunctionalVerificationError(
            f"options.sdf requires engine 'icarus' -- engine '{engine}' has no "
            "SDF back-annotation path ($sdf_annotate is an Icarus-only entry "
            "point through this flow)"
        )

    unknown = sorted(set(sdf) - {"file", "corner", "entries"})
    if unknown:
        raise FunctionalVerificationError(
            "request.options.sdf has unknown field(s): "
            + ", ".join(unknown)
            + " (supported: file, corner, entries)"
        )

    file_value = sdf.get("file")
    if not isinstance(file_value, str) or not file_value:
        raise FunctionalVerificationError(
            "request.options.sdf.file must be a non-empty string"
        )
    path = (
        file_value
        if os.path.isabs(file_value)
        else os.path.join(request_dir, file_value)
    )
    if not os.path.isfile(path):
        raise FunctionalVerificationError(f"SDF file not found: {file_value}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not read SDF file '{file_value}': {exc}"
        ) from exc

    corner = sdf.get("corner", DEFAULT_SDF_CORNER)
    if corner not in SDF_CORNERS:
        raise FunctionalVerificationError(
            "request.options.sdf.corner must be one of: " + ", ".join(SDF_CORNERS)
        )

    entries = sdf.get("entries", DEFAULT_SDF_ENTRIES)
    if entries not in SDF_ENTRIES_MODES:
        raise FunctionalVerificationError(
            "request.options.sdf.entries must be one of: "
            + ", ".join(SDF_ENTRIES_MODES)
        )

    return {"file": os.path.abspath(path), "corner": corner, "entries": entries}


def _write_sdf_annotate_shim(path: str, *, sdf_paths: list[str], scope: str) -> None:
    """Write the extra elaboration root that carries the ``$sdf_annotate``
    call(s) (spike §2.1, verified live; the same shape cocotb's own Icarus
    runner generates for waveform dumping).

    Every path in ``sdf_paths`` must already be absolute -- ``vvp`` runs with
    its own working directory, so a relative path here is one ``SDF
    WARNING`` away from a silent zero-delay run. ``scope`` is the Verilog
    hierarchical reference ``$sdf_annotate``'s SDF paths resolve relative to
    -- since issue #1056, this is **not** ``hdl_toplevel`` directly, but
    ``<SDF_WRAPPER_MODULE>.<SDF_WRAPPER_DUT_INSTANCE>``: the DUT nested one
    level under the generated pass-through wrapper (see
    :func:`_write_sdf_dut_wrapper`), which is what lets Icarus resolve a
    bare top-level-port ``INTERCONNECT`` endpoint at all. The DUT's own
    hierarchy (the thing the Python testbench addresses as ``dut.<port>``,
    now via the wrapper's identically-named ports) is untouched either way.

    ``sdf_paths`` normally has exactly one entry (the caller's own SDF file,
    unmodified). It carries a *second* entry only when
    :func:`_split_sdf_bus_port_interconnects` found a bit-selected
    top-level-port ``INTERCONNECT`` entry to defer to its own, later call
    (issue #1619) -- in which case both ``$sdf_annotate`` calls run
    sequentially in the same ``initial`` block, in the order given, which is
    exactly what makes the deferral work: nothing in the second file's own
    entries runs *after* the poisoning entry on its own net, so nothing is
    left to fail.
    """
    from .functional_verification import (
        SDF_ANNOTATE_MODULE,
    )

    def _escape(sdf_path: str) -> str:
        return sdf_path.replace("\\", "\\\\").replace('"', '\\"')

    calls = "\n".join(
        f'    $sdf_annotate("{_escape(path)}", {scope});' for path in sdf_paths
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "// Generated by klt functional-verification -- do not edit.\n"
            "// Extra elaboration root carrying the $sdf_annotate call(s),\n"
            "// since a cocotb regression's hdl_toplevel is the DUT itself\n"
            "// and has nowhere to host an initial block (issue #1002).\n"
            f"module {SDF_ANNOTATE_MODULE}();\n"
            f"  initial begin\n{calls}\n  end\n"
            "endmodule\n"
        )


#: Two Verilog module-header conventions :func:`_parse_toplevel_ports`
#: supports: non-ANSI (``module <name>(<bare port names>);``, directions
#: declared separately in the body -- what both `klt synthesize`'s Yosys
#: ``write_verilog`` and `klt place-and-route`'s OpenROAD ``write_verilog``
#: emit, verified against ``tests/corpus/statime/gcd_netlist.v``) and
#: ANSI-style (``module <name>(input wire a, output [7:0] y);``, direction
#: inline per port -- what this module's own hand-authored integration-test
#: fixtures use). A *mixed* header (some ports carry an inline direction,
#: others don't -- Verilog's "inherit the previous port's direction" rule)
#: is rejected rather than guessed at.
_ANSI_PORT_TOKEN_RE = re.compile(
    r"^(input|output|inout)\b(?:\s+(?:reg|wire|signed))*\s*(\[[^\]]+\])?\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*)$"
)


def _parse_toplevel_ports(
    source_paths: list[str], hdl_toplevel: str
) -> list[tuple[str, str, str]]:
    """Return ``[(direction, width_or_empty, name), ...]`` for every port of
    ``hdl_toplevel``'s module declaration, in header order -- the port list
    :func:`_write_sdf_dut_wrapper` needs to re-declare identically on the
    generated wrapper (issue #1056).

    Raises :class:`FunctionalVerificationError` (never mis-parses silently)
    when the module cannot be found, its port list mixes ANSI and non-ANSI
    ports, or a non-ANSI port's direction/width declaration cannot be
    located -- any of which would otherwise produce a wrapper that fails to
    compile or, worse, compiles with the wrong port shape.
    """
    from .functional_verification import FunctionalVerificationError

    header_re = re.compile(
        r"module\s+" + re.escape(hdl_toplevel) + r"\s*\(([^;]*?)\)\s*;", re.DOTALL
    )
    text: str | None = None
    header_match = None
    for path in source_paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                candidate = handle.read()
        except OSError:
            continue
        match = header_re.search(candidate)
        if match is not None:
            text, header_match = candidate, match
            break
    if header_match is None or text is None:
        raise FunctionalVerificationError(
            f"options.sdf could not find a 'module {hdl_toplevel}(...)' "
            "declaration in request.sources to build the SDF top-level-port "
            "workaround wrapper (issue #1056)"
        )

    tokens = [
        " ".join(token.split()).lstrip("\\")
        for token in header_match.group(1).split(",")
        if token.strip()
    ]
    if not tokens:
        raise FunctionalVerificationError(
            f"options.sdf: module '{hdl_toplevel}' declares no ports -- "
            "nothing for the SDF top-level-port workaround wrapper to forward"
        )

    ansi_matches = [_ANSI_PORT_TOKEN_RE.match(token) for token in tokens]
    is_ansi = [match is not None for match in ansi_matches]
    if any(is_ansi) and not all(is_ansi):
        raise FunctionalVerificationError(
            f"options.sdf: module '{hdl_toplevel}' mixes ANSI-style ports "
            "(inline direction keywords) with bare port names -- the SDF "
            "top-level-port workaround (issue #1056) does not resolve "
            "Verilog's 'inherit the previous port's direction' rule for this"
        )

    if all(is_ansi):
        return [
            (match.group(1), (match.group(2) or "").strip(), match.group(3))
            for match in ansi_matches
        ]

    # Non-ANSI: `tokens` are bare port names; direction/width is declared
    # separately in the body, one `input`/`output`/`inout` line per port
    # (Yosys/OpenROAD convention).
    ports: list[tuple[str, str, str]] = []
    for name in tokens:
        decl_re = re.compile(
            r"^[ \t]*(input|output|inout)\b"
            r"(?:[ \t]+(?:reg|wire|signed))*"
            r"[ \t]*(\[[^\]]+\])?[ \t]+" + re.escape(name) + r"[ \t]*;",
            re.MULTILINE,
        )
        decl_match = decl_re.search(text)
        if decl_match is None:
            raise FunctionalVerificationError(
                "options.sdf could not find a direction declaration for "
                f"port '{name}' of module '{hdl_toplevel}' -- cannot build "
                "the SDF top-level-port workaround wrapper (issue #1056)"
            )
        ports.append((decl_match.group(1), (decl_match.group(2) or "").strip(), name))
    return ports


def _write_sdf_dut_wrapper(
    path: str, *, hdl_toplevel: str, ports: list[tuple[str, str, str]]
) -> None:
    """Write a transparent pass-through wrapper module around ``hdl_toplevel``
    -- the client-side fix for issue #1056.

    Icarus's ``$sdf_annotate`` cannot resolve an ``INTERCONNECT`` entry whose
    endpoint is a bare top-level port of a module elaborated as its own
    ``-s`` root (verified live: every purely-internal
    ``INTERCONNECT <inst>.<pin> <inst>.<pin>`` entry resolves fine; every
    entry touching a top-level port fails with ``Could not find
    intermodpath!``/``Could not find net`` -- regardless of whether
    ``$sdf_annotate`` is called from a sibling elaboration root or from
    inside the DUT's own initial block, which rules out this module's
    previous separate-elaboration-root shim as the mechanism). The identical
    bare-port SDF syntax resolves cleanly when the named module is instead a
    *nested child instance* of another root (Icarus's own ``ivtest`` SDF
    regression fixtures use exactly this shape). This wrapper supplies that
    shape without touching the DUT's own netlist, ports, or hierarchy: it
    re-declares ``hdl_toplevel``'s exact port list (``ports``, from
    :func:`_parse_toplevel_ports`), instantiates the unmodified DUT as
    :data:`SDF_WRAPPER_DUT_INSTANCE`, and becomes the new elaboration root in
    its place -- so cocotb's own ``dut.<port>`` handles keep resolving
    unchanged (the wrapper's ports carry the identical names and widths),
    while ``$sdf_annotate``'s scope argument
    (:func:`_write_sdf_annotate_shim`) can now name
    ``<SDF_WRAPPER_MODULE>.<SDF_WRAPPER_DUT_INSTANCE>``, a genuinely nested
    scope. Verified live end-to-end through real cocotb + Icarus 13.0, not
    just raw ``iverilog``/``vvp``.
    """
    from .functional_verification import SDF_WRAPPER_DUT_INSTANCE, SDF_WRAPPER_MODULE

    port_lines = ",\n".join(
        f"  {direction} {(width + ' ') if width else ''}{name}".rstrip()
        for direction, width, name in ports
    )
    connections = ",\n".join(f"    .{name}({name})" for _, _, name in ports)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "// Generated by klt functional-verification -- do not edit.\n"
            "// Transparent pass-through wrapper working around Icarus's\n"
            "// inability to resolve a top-level-port INTERCONNECT entry\n"
            "// against a module elaborated as its own -s root (issue #1056).\n"
            f"module {SDF_WRAPPER_MODULE} (\n{port_lines}\n);\n"
            f"  {hdl_toplevel} {SDF_WRAPPER_DUT_INSTANCE} (\n{connections}\n  );\n"
            "endmodule\n"
        )


# --------------------------------------------------------------------------- #
# Issue #1619: a bit-selected top-level-port `INTERCONNECT` endpoint poisons
# a *sibling* `INTERCONNECT` entry on the same net -- two distinct Icarus
# 13.0 behaviours, one fixed here and one root-caused but not fixable from
# this module.
#
# #1069/#1056 fixed a *scalar* bare top-level-port endpoint (`INTERCONNECT a
# u1.in`, `INTERCONNECT u2.out b`) by nesting the DUT under a transparent
# wrapper. Issue #1619 reports a *sibling*-entry failure that appeared once
# that fix let a bit-selected *vector* port endpoint (`y[0]`, `a[0]` -- what
# any real post-route netlist's bus ports produce) resolve too. The issue
# body's own hypothesis was that the wrapper "loses track of (or
# double-binds)" a net's identity once it resolves one entry against it.
# Live testing (raw `iverilog`/`vvp` 13.0, not just through this module, the
# same methodology #1069's own spike section used -- see
# `docs/design/sdf-annotate-feasibility-spike.md` §3.7) **refutes that
# hypothesis** and finds two distinct mechanisms instead, depending on which
# side of the entry carries the bit-selected port:
#
# Shape (a) -- bit-selected port as a *destination* (an output net fanning
# out to a vector output-port bit *and* an internal pin, or any entry sharing
# a source with one): **order-dependent, and fixed here.** Once an entry
# touching a bit-selected port is annotated, every *later* `INTERCONNECT`
# entry sharing that entry's physical net fails with "Could not find
# intermodpath!" regardless of its own shape (a purely internal
# instance-pin-to-instance-pin entry is equally affected) -- which refutes
# the "identity double-bind" hypothesis on its own: reordering the entries
# (bit-selected-port entry *last* instead of first) makes every entry
# resolve, which a genuine net-identity double-bind would not fix (the same
# physical net is still touched twice either way, just in a different
# order). The workaround, verified live end-to-end through real cocotb +
# Icarus 13.0: defer every bit-selected-top-level-port-touching entry to its
# own, later `$sdf_annotate` call, after every other entry has already been
# annotated. Nothing in the deferred call's own entries runs *after* a
# poisoning entry on that entry's own net (each vector port bit is its own
# physically distinct net), so nothing is left to fail.
#
# Shape (b) -- bit-selected port as a *source* (an input net fanning out to
# an ordinary cell input *and* a single-pin antenna/diode-style load):
# **not order-dependent, and not fixable from here.** A single such entry,
# entirely alone with no sibling at all, already fails whenever its
# destination pin declares its own `specify`/`IOPATH` block (an ordinary
# standard cell always does) -- reordering or deferring it to its own call
# changes nothing, ruling out the same workaround shape (a) uses. It is also
# unexpectedly sensitive to the *testbench*: the identical fixture resolves
# cleanly with a no-op cocotb test (one that returns without ever `await`ing
# a trigger) but deterministically fails once the testbench actually drives
# the DUT and awaits a `Timer` -- reproduced identically across a clean
# `.klt/` build directory, a fresh Python interpreter, and a fresh `klt` CLI
# process, so it is not this module's own build-directory or process-state
# reuse. That points at a genuine Icarus-internal race between
# `$sdf_annotate`'s own intermodpath insertion and VPI callback scheduling
# once the simulator advances past time 0 -- outside what a generated
# wrapper, shim, or SDF split can control. Left unfixed and documented
# (`test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
# unresolvable` pins the realistic, driven-testbench outcome down so it is
# never silently "fixed" by an unrelated future change without the
# mechanism actually being understood).
# --------------------------------------------------------------------------- #

#: A bare top-level-port reference to one bit of a vector port, e.g. ``y[0]``
#: -- never an instance-pin reference (those are always ``<inst>.<pin>``, a
#: shape this regex does not match at all).
_SDF_BUS_PORT_TOKEN_RE = re.compile(r"^([A-Za-z_$][A-Za-z0-9_$]*)\[(\d+)\]$")


def _sdf_matching_paren(text: str, open_index: int) -> int:
    """Return the index of the ``)`` that closes the ``(`` at ``open_index``,
    balancing nested parens -- used throughout this section instead of a
    regex, since an SDF ``(DELAY (ABSOLUTE ...))`` clause nests arbitrarily
    and a non-greedy regex would truncate at the first inner ``)``.
    """
    from .functional_verification import FunctionalVerificationError

    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise FunctionalVerificationError(
        "options.sdf: unbalanced parentheses while scanning the SDF file for "
        "bit-selected top-level-port INTERCONNECT entries (issue #1619) -- "
        "the file is not well-formed SDF"
    )


def _sdf_top_level_clauses(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans (paren-inclusive) for each direct-child
    ``(...)`` clause within ``text[start:end]`` -- e.g. the individual
    ``INTERCONNECT``/``IOPATH``/... entries directly inside a ``(DELAY
    (ABSOLUTE ...))`` clause's body, not any clause nested further inside
    one of them.
    """
    clauses = []
    index = start
    while index < end:
        if text[index] == "(":
            close = _sdf_matching_paren(text, index)
            clauses.append((index, close + 1))
            index = close + 1
        else:
            index += 1
    return clauses


def _split_sdf_bus_port_interconnects(
    sdf_path: str, vector_ports: set[str]
) -> tuple[str, str] | None:
    """Split ``sdf_path`` into ``(deferred_entries_removed_text,
    deferred_entries_only_text)`` when it contains an ``INTERCONNECT`` entry
    whose source or destination is a bit-selected top-level port
    (``<name>[<index>]``, ``name`` in ``vector_ports``) -- or ``None`` if it
    has no such entry, in which case the caller keeps the single-
    ``$sdf_annotate``-call path unchanged (every existing design/test with
    only scalar top-level ports, including #1069's own regression, is
    untouched by this function).

    See the module-level comment above this function for both confirmed
    Icarus 13.0 mechanisms (issue #1619) and which one this actually fixes:
    it reliably resolves shape (a) (bit-selected port as an
    ``INTERCONNECT`` *destination*, order-dependent poisoning of later
    same-net entries) but does **not** fix shape (b) (bit-selected port as
    an ``INTERCONNECT`` *source* feeding a ``specify``-bearing destination,
    which fails even in total isolation, unaffected by this split). This
    function still applies the same split to *any* bit-selected-port-
    touching entry regardless of which shape it is, since doing so never
    makes a shape (b) entry *worse* (it already fails standing alone) and
    fully fixes every shape (a) entry -- there is no cheap way to tell the
    two shapes apart from the SDF text alone (both are just "port token as
    one of the two endpoints"), and splitting is a no-op safety margin for
    the shape this function cannot help.

    The first returned text is ``sdf_path``'s own content with every
    bit-selected-port-touching entry *removed* (annotated in the caller's
    normal, first ``$sdf_annotate`` call, so it never sees one at all); the
    second is a fresh, minimal SDF file carrying *only* those removed
    entries (annotated in a second, later call -- nothing in it runs after
    a shape (a) poisoning entry on that entry's own net, since each vector
    port bit is its own physically distinct net, so nothing is left to
    fail there).

    ``vector_ports`` is empty for the overwhelmingly common case (no bus
    top-level port at all), in which case this returns ``None`` immediately
    without reading the file -- a real post-route SDF can be large, and this
    function's whole cost should be paid only when it can possibly matter.
    """
    if not vector_ports:
        return None
    with open(sdf_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    def _header_field(pattern: str, default: str) -> str:
        match = re.search(pattern, text)
        return match.group(1) if match else default

    sdfversion = _header_field(r'\(SDFVERSION\s+"([^"]*)"\)', "3.0")
    design = _header_field(r'\(DESIGN\s+"([^"]*)"\)', "")
    divider = _header_field(r"\(DIVIDER\s+(\S+?)\)", ".")
    timescale = _header_field(r"\(TIMESCALE\s+([^)]*)\)", "1ns")

    # Group deferred entries by the (celltype, instance) of the CELL block
    # they came from -- almost always exactly one group (the top design's
    # own CELL block, the only place a real `write_sdf` emits INTERCONNECT
    # entries at all), but this does not assume that.
    deferred_by_cell: dict[tuple[str, str], list[str]] = {}
    removed_spans: list[tuple[int, int]] = []

    for cell_match in re.finditer(r"\(CELL\b", text):
        cell_start = cell_match.start()
        cell_end = _sdf_matching_paren(text, cell_start) + 1
        cell_text = text[cell_start:cell_end]
        celltype_match = re.search(r'\(CELLTYPE\s+"([^"]*)"\)', cell_text)
        instance_match = re.search(r"\(INSTANCE\s*([^)]*)\)", cell_text)
        celltype = celltype_match.group(1) if celltype_match else ""
        instance = instance_match.group(1).strip() if instance_match else ""

        delay_match = re.search(r"\(DELAY\b", cell_text)
        if delay_match is None:
            continue
        delay_start = cell_start + delay_match.start()
        delay_end = _sdf_matching_paren(text, delay_start) + 1
        # The DELAY clause's own single child is (ABSOLUTE ...) or
        # (INCREMENT ...); its children are the individual delay entries.
        delay_type_spans = _sdf_top_level_clauses(text, delay_start + 1, delay_end - 1)
        for type_start, type_end in delay_type_spans:
            entry_spans = _sdf_top_level_clauses(text, type_start + 1, type_end - 1)
            for entry_start, entry_end in entry_spans:
                entry_text = text[entry_start:entry_end]
                interconnect_match = re.match(
                    r"\(INTERCONNECT\s+(\S+)\s+(\S+)", entry_text
                )
                if interconnect_match is None:
                    continue
                src, dst = interconnect_match.group(1), interconnect_match.group(2)
                endpoints = (src, dst)
                if not any(
                    (bus_match := _SDF_BUS_PORT_TOKEN_RE.match(token)) is not None
                    and bus_match.group(1) in vector_ports
                    for token in endpoints
                ):
                    continue
                deferred_by_cell.setdefault((celltype, instance), []).append(entry_text)
                removed_spans.append((entry_start, entry_end))

    if not deferred_by_cell:
        return None

    # Build the "safe" text: the original file with every deferred entry's
    # exact span cut out, leaving every surrounding paren/structure (and
    # every non-deferred entry) untouched.
    removed_spans.sort()
    safe_parts = []
    cursor = 0
    for entry_start, entry_end in removed_spans:
        safe_parts.append(text[cursor:entry_start])
        cursor = entry_end
    safe_parts.append(text[cursor:])
    safe_text = "".join(safe_parts)

    cell_blocks = []
    for (celltype, instance), entries in deferred_by_cell.items():
        instance_clause = f"(INSTANCE {instance})" if instance else "(INSTANCE)"
        entries_text = "\n      ".join(entries)
        cell_blocks.append(
            "  (CELL\n"
            f'    (CELLTYPE "{celltype}")\n'
            f"    {instance_clause}\n"
            f"    (DELAY (ABSOLUTE\n      {entries_text}\n    ))\n"
            "  )"
        )

    deferred_text = (
        "(DELAYFILE\n"
        f'  (SDFVERSION "{sdfversion}")\n'
        f'  (DESIGN "{design}")\n'
        f"  (DIVIDER {divider})\n"
        f"  (TIMESCALE {timescale})\n" + "\n".join(cell_blocks) + "\n)\n"
    )

    return safe_text, deferred_text


# --------------------------------------------------------------------------- #
# Issue #2285: a zero-delay `INTERCONNECT` entry whose *destination* is a
# top-level output port bit the gate-level netlist drives through a Verilog
# `assign` alias -- the shape every P&R backend produces for a
# constant-driven output:
#
#     sg13cmos5l_tielo const_drive_0 (.L_LO(net0));
#     assign uio_oe[0] = net0;
#     (INTERCONNECT const_drive_0.L_LO uio_oe[0] (0.000:0.000:0.000))
#
# Icarus 13.0 cannot insert an intermodpath across that `assign` join, so
# every such entry costs one `SDF ERROR: <file>:<line>: Could not find
# intermodpath!` -- and since the diagnostic gate (correctly) treats an
# unapplied annotation as a hard failure, a real post-route SDF could not
# pass the gate at all, even though every failing entry was
# `(0.000:0.000:0.000)` and therefore modelled no delay whatsoever.
#
# Two resolutions were tried live against raw `iverilog`/`vvp` 13.0 (the
# same methodology #1069/#1619 used -- see
# `docs/design/sdf-annotate-feasibility-spike.md`):
#
# 1. **Rewrite the endpoint through the netlist's alias map** (resolve
#    `uio_oe[0]` to its canonical net `net0` before annotation, so the entry
#    actually applies): **refuted.** The rewritten entry names the very net
#    its own source pin drives -- a degenerate zero-length interconnect --
#    and Icarus then fails it with a *different* diagnostic (`Could not find
#    handles for both ports!`) instead of resolving it. There is no
#    endpoint spelling that makes an entry across an `assign` join
#    insertable.
# 2. **Drop the entry, with a counted exemption** (what is implemented
#    here): remove it from the SDF text handed to `$sdf_annotate` and report
#    the count as an `environment.sdf.dropped` class, so the run is
#    machine-distinguishable from an ordinary clean annotation.
#
# The exemption is bounded by the delay value, not by the diagnostic text,
# which is what keeps it from ever masking a real annotation failure: an
# entry is dropped only when *every* delay value it carries (all three
# `min:typ:max` members of every rvalue, not just the `-T`-selected one) is
# zero, so dropping it cannot change any simulated timing at any corner. A
# **non**-zero-delay entry on the identical alias-aliased port bit is left
# in place, fails to annotate exactly as before, and still raises
# `FunctionalVerificationError` -- loud, not silently retimed.
#
# Keying on the *entry's* delay value rather than on the diagnostic line is
# also the only workable shape: Icarus's `SDF ERROR: <file>:<line>:` locator
# cannot be mapped back to an entry once more than one `$sdf_annotate` call
# is in play, because its line counter is **cumulative across calls** rather
# than reset per file (verified live: with #1619's two-call split, an entry
# on line 10 of the second file is reported as line 24 -- 14 lines of the
# first file plus 10).
#
# The removal cleans up after itself structurally, because Icarus's SDF
# parser rejects an emptied clause outright (verified live): a `(DELAY
# (ABSOLUTE))` with no entries left is `Invalid/malformed delay type`, and a
# `(DELAYFILE ...)` with no `CELL` at all is `Invalid DELAYFILE format`. So
# emptying a delay-type clause removes that clause, emptying every
# delay-type clause of a `(DELAY ...)` removes the `(DELAY ...)`, and the
# `(CELL ...)` itself is always kept (a `CELL` carrying only `CELLTYPE`/
# `INSTANCE` and no `DELAY` annotates cleanly).
# --------------------------------------------------------------------------- #

#: Every numeric literal in an SDF delay value list -- integers, decimals,
#: and exponent forms, signed or not. :func:`_sdf_interconnect_is_zero_delay`
#: applies this to the *delay* portion of an ``INTERCONNECT`` entry only
#: (never to the endpoint tokens, whose bit selects carry digits of their
#: own), and requires every match to be zero.
_SDF_DELAY_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

#: The head of an ``INTERCONNECT`` entry: the keyword plus its two endpoint
#: tokens (source, destination). Everything past the match is the entry's
#: delay value list.
_SDF_INTERCONNECT_HEAD_RE = re.compile(r"\(INTERCONNECT\s+(\S+)\s+(\S+)")


def _collect_sdf_alias_port_bits(
    source_paths: list[str], hdl_toplevel: str
) -> set[str]:
    """Every top-level port (bit-expanded: ``uio_oe[0]``, or a bare scalar
    port name) of ``hdl_toplevel`` whose only Verilog-level connection is an
    ``assign`` alias -- the port references an SDF ``INTERCONNECT``
    destination can name but Icarus cannot insert an intermodpath onto
    (issue #2285).

    Reuses :func:`klayout_tools.verilog_netlist.collect_gate_level_port_aliases`
    (issue #2021's own alias map, already reused by
    ``klayout_tools.lvs._apply_gate_level_port_aliases``) rather than
    re-parsing ``assign`` statements a third time -- but applies it to
    *only* ``hdl_toplevel``'s own ``module``/``endmodule`` chunk, never to
    the whole of ``request.sources``: a real request's sources also carry
    the PDK's behavioural cell models (``specify`` blocks, ``always``
    blocks, non-alias continuous assignments), none of which that
    deliberately narrow gate-level grammar models.

    Returns an empty set -- never raises -- whenever the alias map cannot be
    derived: no such module in any source, an unreadable file, or a top
    module outside that gate-level subset (notably an **ANSI-style** module
    header, ``module top (input a, output [1:0] y);`` -- ``verilog_netlist``
    supports only the non-ANSI ``module top (a, y); input a; output [1:0]
    y;`` form, which is what every real ``write_verilog`` post-route netlist
    -- the only kind that carries this issue's tie-cell shape -- emits).
    An empty set makes
    :func:`_drop_sdf_zero_delay_alias_port_interconnects` a no-op, i.e. the
    exact behaviour this path had before #2285: this is a *normalization*
    pass, and failing to normalize must never be worse than not having
    tried.
    """
    from .verilog_netlist import collect_gate_level_port_aliases

    chunk_re = re.compile(
        r"\bmodule\s+" + re.escape(hdl_toplevel) + r"\b.*?\bendmodule\b",
        re.DOTALL,
    )
    for path in source_paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        chunk_match = chunk_re.search(text)
        if chunk_match is None:
            continue
        try:
            aliases = collect_gate_level_port_aliases(chunk_match.group(0))
        except Exception:
            # Not parseable as gate-level Verilog (a hand-written RTL top,
            # a construct outside verilog_netlist's narrow grammar) -- no
            # alias map, so no normalization. Never fatal.
            return set()
        return set(aliases.get(hdl_toplevel, {}))
    return set()


def _sdf_interconnect_is_zero_delay(entry_text: str) -> bool:
    """``True`` when every delay value in the ``INTERCONNECT`` entry
    ``entry_text`` is zero -- all three ``min:typ:max`` members of every
    rvalue, not just the one ``iverilog -T <corner>`` selects.

    Requiring *all* members (rather than only the requested corner's) is
    deliberately conservative: it is what lets
    :func:`_drop_sdf_zero_delay_alias_port_interconnects`'s exemption be
    stated as "dropping this entry cannot change simulated timing at any
    corner", with no dependence on which corner this particular run asked
    for. An entry carrying no delay value at all is zero-delay by the same
    reasoning (it annotates nothing).

    ``False`` for text that is not an ``INTERCONNECT`` entry at all, so a
    caller can never widen the exemption by mis-classifying an
    ``IOPATH``/``PORT``/``DEVICE`` entry as zero-delay.
    """
    head = _SDF_INTERCONNECT_HEAD_RE.match(entry_text)
    if head is None:
        return False
    return all(
        float(value) == 0.0
        for value in _SDF_DELAY_NUMBER_RE.findall(entry_text[head.end() :])
    )


def _sdf_interconnect_is_droppable(entry_text: str, alias_port_bits: set[str]) -> bool:
    """``True`` when ``entry_text`` is an ``INTERCONNECT`` entry whose
    *destination* endpoint is one of ``alias_port_bits`` **and** whose every
    delay value is zero -- the sole condition
    :func:`_drop_sdf_zero_delay_alias_port_interconnects` removes an entry
    on (issue #2285). Both halves are load-bearing: the destination match
    narrows the pass to the one shape Icarus structurally cannot annotate,
    and the zero-delay test is what makes removing it provably unable to
    change simulated timing.
    """
    head = _SDF_INTERCONNECT_HEAD_RE.match(entry_text)
    if head is None or head.group(2) not in alias_port_bits:
        return False
    return _sdf_interconnect_is_zero_delay(entry_text)


def _sdf_cell_delay_spans(
    text: str, cell_start: int
) -> tuple[tuple[int, int], list[tuple[int, int]]] | None:
    """``((DELAY clause span), [delay-type clause spans])`` for the ``(CELL``
    opening at ``cell_start``, or ``None`` when the cell carries no
    ``(DELAY ...)`` clause -- or when the SDF text around it is malformed.

    A malformed cell is skipped rather than reported: diagnosing a
    not-well-formed SDF belongs to the simulator's own parser (and to
    :func:`_split_sdf_bus_port_interconnects`, which raises on unbalanced
    parens), not to a normalization pass that must never be able to turn a
    working run into a failing one.
    """
    try:
        cell_end = _sdf_matching_paren(text, cell_start) + 1
        delay_match = re.search(r"\(DELAY\b", text[cell_start:cell_end])
        if delay_match is None:
            return None
        delay_start = cell_start + delay_match.start()
        delay_end = _sdf_matching_paren(text, delay_start) + 1
        return (
            (delay_start, delay_end),
            _sdf_top_level_clauses(text, delay_start + 1, delay_end - 1),
        )
    except Exception:
        return None


def _sdf_cell_entry_removals(
    text: str, cell_start: int, droppable: Callable[[str], bool]
) -> tuple[list[tuple[int, int]], int]:
    """``([spans to cut], how many entries that accounts for)`` for one
    ``(CELL ...)`` block -- the per-cell half of every counted-exemption drop
    pass in this module (issue #2285's alias-port drop and issue #2363's
    physical-only-destination drop both route through here), including the
    structural cleanup.

    ``droppable`` is the only thing that differs between the passes: it is
    handed one delay entry's full text and returns whether that entry is to
    be removed. Everything else -- clause walking, counting, and the emptied-
    clause cleanup Icarus's own parser forces -- is identical for both, so it
    lives here once rather than being copied per pass.

    The returned spans are not always the entry spans themselves: emptying a
    delay-type clause returns *that clause's* span instead (Icarus rejects a
    ``(DELAY (ABSOLUTE))`` with no entries as ``Invalid/malformed delay
    type``), and emptying every delay-type clause of a ``(DELAY ...)``
    returns the ``(DELAY ...)`` span. The ``(CELL ...)`` itself is never
    returned: a cell carrying only ``CELLTYPE``/``INSTANCE`` annotates
    cleanly, while a ``(DELAYFILE ...)`` with no ``CELL`` at all is
    ``Invalid DELAYFILE format``. The count is always the number of
    *entries* dropped, however they were cut.
    """
    spans = _sdf_cell_delay_spans(text, cell_start)
    if spans is None:
        return [], 0
    delay_span, type_spans = spans

    removals: list[tuple[int, int]] = []
    emptied_type_spans: list[tuple[int, int]] = []
    dropped = 0
    for type_start, type_end in type_spans:
        entry_spans = _sdf_top_level_clauses(text, type_start + 1, type_end - 1)
        doomed = [
            (start, end) for start, end in entry_spans if droppable(text[start:end])
        ]
        if not doomed:
            continue
        dropped += len(doomed)
        if len(doomed) == len(entry_spans):
            emptied_type_spans.append((type_start, type_end))
        else:
            removals.extend(doomed)

    if not emptied_type_spans:
        return removals, dropped
    if len(emptied_type_spans) == len(type_spans):
        removals.append(delay_span)
    else:
        removals.extend(emptied_type_spans)
    return removals, dropped


def _cut_sdf_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with each ``(start, end)`` span in ``spans`` cut out, leaving
    every surrounding paren, clause, and untouched entry byte-identical."""
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _read_sdf_text(sdf_path: str) -> str | None:
    """``sdf_path``'s content, or ``None`` when it cannot be read. An
    unreadable file is *not* an error on this path: every caller is a
    normalization pass, and failing to normalize must never be worse than
    never having tried."""
    try:
        with open(sdf_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


#: The three SDF header fields whose value may be a ``min:typ:max`` triple
#: (IEEE 1497 §5.3: ``VOLTAGE`` and ``TEMPERATURE`` take an ``rtriple``,
#: ``PROCESS`` a quoted string OpenSTA writes in the same shape). OpenSTA's
#: own ``write_sdf`` emits each as ``min::max`` with an *empty* typ member
#: (issue #1880), which ``iverilog -T typ`` rejects with ``SDF ERROR: ...
#: Chosen value not defined.`` even though Icarus uses none of the three for
#: delay selection.
_SDF_HEADER_TRIPLE_FIELD_RE = re.compile(
    r'\((?P<field>VOLTAGE|TEMPERATURE|PROCESS)(?P<sep>\s+)(?P<quote>"?)'
    r'(?P<value>[^()":]*:[^()":]*:[^()":]*)(?P=quote)\s*\)'
)


def _fill_sdf_triple(value: str) -> str:
    """``value`` (an ``a:b:c`` triple, members possibly empty) with every
    empty member filled from a populated neighbour -- typ from min (else
    max), min/max from typ (else the other extreme) -- so ``1.8::1.8``
    becomes ``1.8:1.8:1.8`` and ``-40::125`` becomes ``-40:-40:125``. An
    all-empty triple has nothing to fill from and is returned unchanged."""
    lo, typ, hi = (member.strip() for member in value.split(":"))
    if not (lo or typ or hi):
        return value
    typ = typ or lo or hi
    lo = lo or typ
    hi = hi or typ
    return f"{lo}:{typ}:{hi}"


def _normalize_sdf_header_triples(sdf_path: str) -> str | None:
    """``sdf_path``'s text with any partially-empty ``VOLTAGE``/
    ``TEMPERATURE``/``PROCESS`` header triple filled in -- or ``None`` when
    there is nothing to fill (or the file cannot be read), in which case the
    caller keeps handing ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    Issue #1880: OpenSTA's ``write_sdf`` writes those three header fields as
    ``min::max`` triples, and ``iverilog -T typ`` (the default
    ``options.sdf.corner``) rejects each one with ``Chosen value not
    defined`` -- an ``SDF ERROR`` the transcript gate correctly turns into a
    hard failure, so the documented ``klt place-and-route`` -> ``klt
    functional-verification`` handoff failed on every real design. Icarus
    uses none of the three fields for delay selection, so filling the empty
    member cannot change any simulated delay.

    Scoped strictly to the header -- the text before the first ``(CELL``
    -- and to those three fields: a delay entry with a missing triple member
    is a real problem and is left untouched, for Icarus to report loudly.
    """
    text = _read_sdf_text(sdf_path)
    if text is None:
        return None
    cell = re.search(r"\(CELL\b", text)
    header_end = cell.start() if cell else len(text)
    header = text[:header_end]

    def _fill(match: re.Match[str]) -> str:
        value = match.group("value")
        filled = _fill_sdf_triple(value)
        if filled == value:
            return match.group(0)
        start, end = match.span("value")
        offset = match.start()
        whole = match.group(0)
        return whole[: start - offset] + filled + whole[end - offset :]

    normalized = _SDF_HEADER_TRIPLE_FIELD_RE.sub(_fill, header)
    if normalized == header:
        return None
    return normalized + text[header_end:]


def _drop_sdf_interconnect_entries(
    text: str, droppable: Callable[[str], bool]
) -> tuple[str, int] | None:
    """``(``text`` with every ``droppable`` delay entry removed, how many
    were removed)`` -- or ``None`` when nothing matched, in which case the
    caller keeps handing its own input to ``$sdf_annotate`` unchanged.

    The shared core of this module's counted-exemption drop passes (issues
    #2285 and #2363): each supplies its own per-entry ``droppable``
    predicate and gets the same clause walking, counting, and emptied-clause
    cleanup.
    """
    removed_spans: list[tuple[int, int]] = []
    dropped = 0
    for cell_match in re.finditer(r"\(CELL\b", text):
        spans, count = _sdf_cell_entry_removals(text, cell_match.start(), droppable)
        removed_spans.extend(spans)
        dropped += count

    if not dropped:
        return None
    return _cut_sdf_spans(text, removed_spans), dropped


def _drop_sdf_non_interconnect_entries(sdf_path: str) -> tuple[str, int] | None:
    """``(``sdf_path``'s text with every non-``INTERCONNECT`` delay entry
    removed, how many were removed)`` -- or ``None`` when there is nothing
    to remove (or the file cannot be read), in which case the caller keeps
    handing ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    The ``options.sdf.entries: "interconnect"`` mode's whole implementation
    (issue #2364): the net-delay back-annotation configuration that issue's
    isolation table validated on a timing-clean sky130A design -- a design
    whose SPEF-annotated STA closes both setup and hold with positive slack
    still simulates correctly with ``INTERCONNECT``-only annotation, while
    any SDF carrying ``IOPATH`` module-path delays kills the same design
    even at a 10x-relaxed clock period, which a genuine timing failure on a
    positive-slack design cannot produce. The suspected mechanism is the
    sky130 specify-branch models' ``*_delayed`` nets interacting with
    Icarus's module-path-delay scheduling; whatever its exact shape, the
    entry class is the discriminator, so the supported configuration is the
    one that annotates the surviving class honestly rather than leaving
    callers to hand-filter SDF text off-tool.

    Every delay entry whose text does not start with ``(INTERCONNECT`` is
    dropped -- on a real ``write_sdf`` output that is the ``IOPATH``
    population and nothing else. The count is the number of *entries*
    removed, and an emptied ``(DELAY ...)`` clause or delay-type clause is
    removed along with its entries by the shared
    :func:`_sdf_cell_entry_removals` cleanup (a ``(DELAY (ABSOLUTE))`` with
    no entries is itself an Icarus parse error), so the filtered text this
    returns is one Icarus accepts unchanged. ``(TIMINGCHECK ...)`` sections
    are *not* touched here: Icarus drops every one of them itself (the
    ``timingcheck`` ``environment.sdf.dropped`` class, issue #1102), which
    is already counted and reported honestly without this pass needing to
    re-implement it.
    """
    text = _read_sdf_text(sdf_path)
    if text is None:
        return None
    return _drop_sdf_interconnect_entries(
        text, lambda entry: not entry.lstrip().startswith("(INTERCONNECT")
    )


def _drop_sdf_zero_delay_alias_port_interconnects(
    sdf_path: str, alias_port_bits: set[str]
) -> tuple[str, int] | None:
    """Remove every zero-delay ``INTERCONNECT`` entry in ``sdf_path`` whose
    *destination* is one of ``alias_port_bits``, returning ``(text with
    those entries removed, how many were removed)`` -- or ``None`` when
    there is nothing to remove, in which case the caller keeps handing
    ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    See the module-level comment above for issue #2285's live findings: why
    the entry cannot be made to annotate by rewriting its endpoint, why the
    exemption is keyed on the entry's own delay value rather than on
    Icarus's diagnostic line, and why an emptied clause has to be removed
    along with its entries.

    ``alias_port_bits`` is empty for every design with no ``assign``-aliased
    top-level port (the overwhelmingly common case), in which case this
    returns ``None`` immediately without reading the file -- a real
    post-route SDF can be large, and this pass's whole cost should be paid
    only when it can possibly matter.

    Only the *destination* endpoint is matched. That is the side every
    observed failure carries (a constant driver's output pin feeding an
    aliased output port bit), and the source side has its own separate,
    unrelated Icarus limitation already pinned down by
    ``test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
    unresolvable`` (issue #1619 shape (b)) -- widening this pass to sources
    would silently take over that documented case without evidence that
    dropping is the right answer there. (Issue #2363 supplies that evidence
    for one strictly narrower subset of shape (b) -- a bit-selected-port
    source feeding a *physical-only*, modpath-free destination instance --
    and handles it in its own separate pass,
    :func:`_drop_sdf_zero_delay_physical_only_interconnects`, precisely so
    the general shape (b) case here stays untouched and still fails loudly.)
    """
    if not alias_port_bits:
        return None
    text = _read_sdf_text(sdf_path)
    if text is None:
        return None
    return _drop_sdf_interconnect_entries(
        text,
        lambda entry_text: _sdf_interconnect_is_droppable(entry_text, alias_port_bits),
    )


# --------------------------------------------------------------------------- #
# Issue #2363: a zero-delay `INTERCONNECT` entry whose *destination* is a pin
# of a **physical-only** instance -- a cell the router placed for a physical
# reason with no timing content of its own, the antenna-effect diode being
# the canonical case:
#
#     sky130_fd_sc_hd__diode_2 ANTENNA_diode_0 (.DIODE(ui_in[3]));
#     (INTERCONNECT ui_in[3] ANTENNA_diode_0.DIODE (0.000:0.000:0.000))
#
# This is shape (b) of the #1619/#1857 spike (`docs/design/sdf-annotate-
# feasibility-spike.md` §3.7) -- a bit-selected top-level input port as the
# entry's *source* -- so neither #1619's deferral nor #2285's alias-map
# rewrite resolves it. It costs an `SDF ERROR: <file>:<line>: Could not find
# intermodpath!`, and the diagnostic gate (correctly) treats an unapplied
# annotation as a hard failure, so a real post-route SDF whose router
# inserted antenna diodes on top-level input bits could not pass the gate at
# all.
#
# Reproduced live against Icarus 13.0 through this module, which pinned down
# a condition the issue's own write-up does not state: the failure needs the
# port bit's net to carry **more than one** `INTERCONNECT` entry. A bus-port
# net whose only entry is the diode's own annotates cleanly even without this
# pass (#1619's deferral is enough, since each port bit is its own net), but
# the moment that net carries a second entry one of them fails -- exactly the
# same-net poisoning #1619 shape (a) describes, which the deferral cannot
# undo because both entries are on the one net it deferred. A real router
# inserts one diode per violating pin, so a multi-load input bit collects
# several of these.
#
# What makes *this* subset of shape (b) different -- and what #2285's
# docstring said was missing before the drop could be widened to a source-
# side match at all -- is that the destination here is structurally
# incapable of carrying a modpath: the diode model has no `specify` block
# and no output port whatsoever (the no-`USE_POWER_PINS` variant is a module
# with a single `input` and an empty body). There is no modpath the entry
# could ever annotate, and no logic anywhere that observes the annotated
# pin. The general shape (b) case -- a bit-selected-port source feeding an
# ordinary `specify`-bearing standard cell -- is emphatically *not* covered
# here: that destination has real modpaths, dropping its entry would change
# simulated timing, and it must keep failing loudly exactly as
# `test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
# unresolvable` documents. Verified live, not merely asserted: on a net
# carrying both a diode entry and a `specify`-bearing entry, dropping the
# diode entry leaves the ordinary entry behind and the run still raises.
#
# The exemption carries #2285's own bound verbatim, and for the same reason:
# an entry is dropped only when *every* delay member it carries (all three
# `min:typ:max` members of every rvalue, not just the `-T`-selected one) is
# zero, so dropping it cannot change any simulated timing at any corner. A
# non-zero-delay entry onto the identical physical-only pin is left in
# place, fails to annotate, and still raises -- loud, not silently retimed.
#
# "Physical-only" is decided *structurally*, from the elaborated Verilog the
# run is already compiling (:func:`_collect_sdf_physical_only_instances`),
# never from a cell-name pattern like `*_diode_*`: a name pattern is a
# per-PDK naming-convention coupling this repo avoids elsewhere, and it
# cannot tell a timing-empty diode from a `specify`-bearing cell that merely
# has "diode" in its name. Every ambiguity resolves toward *not* dropping --
# a model that cannot be found, a top module outside `verilog_netlist`'s
# gate-level grammar, or any `output`/`inout` declaration or `specify` block
# anywhere in the model -- so a mis-parse can only ever leave an entry in
# place to fail loudly, never silently remove a real delay.
# --------------------------------------------------------------------------- #

#: A ``module <name> ... endmodule`` block for a *named* cell, matched
#: non-greedily so the first ``endmodule`` closes it. Used by
#: :func:`_sdf_cell_model_is_physical_only` to find a destination instance's
#: model text among the run's own sources.
_VERILOG_MODULE_CHUNK_TEMPLATE = r"\bmodule\s+{name}\b.*?\bendmodule\b"

#: Any declaration that disqualifies a model from being "physical-only".
#: ``specify`` is the direct test (no ``specify`` block, no modpath -- which
#: is the whole justification for the drop); ``output``/``inout`` are the
#: conservative belt-and-braces half, since a cell with a driven pin is one
#: whose annotation could conceivably be observed even without a modpath.
_VERILOG_TIMING_CAPABLE_RE = re.compile(r"\b(?:specify|output|inout)\b")

#: Verilog comments, stripped before :data:`_VERILOG_TIMING_CAPABLE_RE` is
#: applied so a model whose header comment merely mentions "output" is not
#: mis-classified as timing-capable.
_VERILOG_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _sdf_cell_model_is_physical_only(cell: str, source_texts: list[str]) -> bool:
    """``True`` when every definition of Verilog module ``cell`` found in
    ``source_texts`` declares no ``specify`` block, no ``output`` port, and
    no ``inout`` port -- a cell that is structurally incapable of carrying a
    modpath (issue #2363).

    ``False`` when no definition is found at all, and ``False`` the moment
    any definition is timing-capable. Both defaults are deliberate: this
    predicate only ever authorises *removing* an SDF entry, so every
    uncertainty has to resolve toward leaving the entry in place, where it
    fails loudly rather than silently disappearing.
    """
    pattern = re.compile(
        _VERILOG_MODULE_CHUNK_TEMPLATE.format(name=re.escape(cell)), re.DOTALL
    )
    found = False
    for text in source_texts:
        for chunk_match in pattern.finditer(text):
            found = True
            chunk = _VERILOG_COMMENT_RE.sub(" ", chunk_match.group(0))
            if _VERILOG_TIMING_CAPABLE_RE.search(chunk):
                return False
    return found


def _read_verilog_source_texts(source_paths: list[str]) -> list[str]:
    """Every file in ``source_paths`` that can be read, in order, silently
    skipping one that cannot -- the same missing-source tolerance this
    module's other normalization passes apply, since failing to read one
    source must never be fatal.
    """
    source_texts: list[str] = []
    for path in source_paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                source_texts.append(handle.read())
        except OSError:
            continue
    return source_texts


def _find_gate_level_instances(
    source_texts: list[str], hdl_toplevel: str
) -> list[dict[str, Any]]:
    """``hdl_toplevel``'s own instance list, read out of the first of
    ``source_texts`` that contains a ``module <hdl_toplevel> ... endmodule``
    chunk -- the per-source half of
    :func:`_collect_sdf_physical_only_instances`, split out on its own so
    that function's cyclomatic complexity stays low.

    Returns an empty list -- never raises -- whenever the instance list
    cannot be derived: no such module in any source, or a top module outside
    :func:`klayout_tools.verilog_netlist.parse_gate_level_verilog`'s narrow
    gate-level grammar (notably an **ANSI-style** module header, which that
    grammar does not model).
    """
    from .verilog_netlist import parse_gate_level_verilog

    chunk_re = re.compile(
        _VERILOG_MODULE_CHUNK_TEMPLATE.format(name=re.escape(hdl_toplevel)), re.DOTALL
    )
    for text in source_texts:
        chunk_match = chunk_re.search(text)
        if chunk_match is None:
            continue
        try:
            modules = parse_gate_level_verilog(chunk_match.group(0))
        except Exception:
            # Not parseable as gate-level Verilog (a hand-written RTL top, a
            # construct outside verilog_netlist's narrow grammar) -- no
            # instance list. Never fatal.
            return []
        for module in modules:
            if module["name"] == hdl_toplevel:
                return list(module["instances"])  # type: ignore[arg-type]
        return []
    return []


def _collect_sdf_physical_only_instances(
    source_paths: list[str], hdl_toplevel: str
) -> set[str]:
    """Every instance name in ``hdl_toplevel`` whose cell model is
    physical-only -- no ``specify`` block, no ``output``/``inout`` pin, so no
    modpath an ``INTERCONNECT`` entry onto one of its pins could ever
    annotate (issue #2363). Antenna-effect diodes are the case this exists
    for; a filler/tapcell with a timing-empty model qualifies by the same
    construction.

    Reuses :func:`klayout_tools.verilog_netlist.parse_gate_level_verilog`'s
    instance list -- the same parser
    :func:`_collect_sdf_alias_port_bits` already reuses for #2285's alias
    map -- applied to *only* ``hdl_toplevel``'s own ``module``/``endmodule``
    chunk (:func:`_find_gate_level_instances`), never to the whole of
    ``request.sources``: a real request's sources also carry the PDK's
    behavioural cell models (``specify`` blocks, ``always`` blocks,
    non-alias continuous assignments), none of which that deliberately
    narrow gate-level grammar models. The *cell models* are then classified
    by plain text scan (:func:`_sdf_cell_model_is_physical_only`), which is
    exactly what that grammar cannot parse.

    Returns an empty set -- never raises -- whenever the instance list
    cannot be derived (see :func:`_find_gate_level_instances`): an empty
    instance list makes
    :func:`_drop_sdf_zero_delay_physical_only_interconnects` a no-op, i.e.
    the exact behaviour this path had before #2363: this is a
    *normalization* pass, and failing to normalize must never be worse than
    not having tried.
    """
    source_texts = _read_verilog_source_texts(source_paths)
    instances = _find_gate_level_instances(source_texts, hdl_toplevel)

    physical_only: dict[str, bool] = {}
    names: set[str] = set()
    for instance in instances:
        cell = str(instance["cell"])
        if cell not in physical_only:
            physical_only[cell] = _sdf_cell_model_is_physical_only(cell, source_texts)
        if physical_only[cell]:
            names.add(str(instance["name"]))
    return names


def _sdf_interconnect_is_physical_only_droppable(
    entry_text: str,
    bus_input_ports: set[str],
    physical_only_instances: set[str],
    divider: str,
) -> bool:
    """``True`` when ``entry_text`` is an ``INTERCONNECT`` entry whose
    *source* is a bit-selected top-level input port in ``bus_input_ports``,
    whose *destination* is a pin of an instance in
    ``physical_only_instances``, and whose every delay member is zero -- the
    sole condition :func:`_drop_sdf_zero_delay_physical_only_interconnects`
    removes an entry on (issue #2363).

    All three halves are load-bearing. The destination match is the
    justification (no ``specify`` block and no output pin means no modpath
    the entry could annotate and no logic that observes the pin); the
    zero-delay test is what makes removing it provably unable to change
    simulated timing at any corner; and the bit-selected-input-port source
    match is what keeps the exemption pinned to the one shape #2363 actually
    observed failing, rather than quietly generalising to every entry that
    happens to land on a physical-only pin.

    ``divider`` is the SDF file's own ``(DIVIDER ...)`` character, so the
    destination is split on the separator *that file* declares rather than a
    hardcoded ``.``. Only a single-level ``<instance><divider><pin>``
    destination matches: a deeper hierarchical path names an instance that
    is not in ``hdl_toplevel``'s own instance list, so it is left alone.
    """
    head = _SDF_INTERCONNECT_HEAD_RE.match(entry_text)
    if head is None:
        return False
    source_match = _SDF_BUS_PORT_TOKEN_RE.match(head.group(1))
    if source_match is None or source_match.group(1) not in bus_input_ports:
        return False
    instance, separator, _pin = head.group(2).rpartition(divider)
    if not separator or instance not in physical_only_instances:
        return False
    return _sdf_interconnect_is_zero_delay(entry_text)


def _drop_sdf_zero_delay_physical_only_interconnects(
    sdf_path: str,
    bus_input_ports: set[str],
    physical_only_instances: set[str],
) -> tuple[str, int] | None:
    """Remove every zero-delay ``INTERCONNECT`` entry in ``sdf_path`` that
    runs from a bit-selected top-level input port in ``bus_input_ports``
    onto a pin of a physical-only instance in ``physical_only_instances``,
    returning ``(text with those entries removed, how many were removed)``
    -- or ``None`` when there is nothing to remove, in which case the caller
    keeps handing ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    See the module-level comment above for issue #2363's findings: why this
    subset of the spike's shape (b) is structurally un-annotatable rather
    than merely order-dependent, why the general shape (b) case is
    deliberately left failing, and why "physical-only" is decided from the
    elaborated model rather than from a cell-name pattern.

    Either empty set makes this a no-op that returns ``None`` immediately
    without reading the file -- the overwhelmingly common case (no bus
    top-level port, or no timing-empty cell in the netlist), and a real
    post-route SDF can be large enough that this pass's whole cost should be
    paid only when it can possibly matter.
    """
    if not bus_input_ports or not physical_only_instances:
        return None
    text = _read_sdf_text(sdf_path)
    if text is None:
        return None
    divider_match = re.search(r"\(DIVIDER\s+(\S+?)\)", text)
    divider = divider_match.group(1) if divider_match else "."

    return _drop_sdf_interconnect_entries(
        text,
        lambda entry_text: _sdf_interconnect_is_physical_only_droppable(
            entry_text, bus_input_ports, physical_only_instances, divider
        ),
    )


# --------------------------------------------------------------------------- #
# Issue #2885: an `IOPATH` entry onto a cell arc whose *only* module paths in
# the cell's Verilog model are `ifnone` + edge-sensitive -- the shape the
# gf180mcu standard-cell models give every non-unate arc (XOR/XNOR inputs,
# MUX select):
#
#     ifnone
#      (posedge A1 => (Z:A1)) = (1.0,1.0);
#     ifnone
#      (negedge A1 => (Z:A1)) = (1.0,1.0);
#
# OpenSTA's `write_sdf` writes that arc as two unqualified entries, one per
# unateness sense (`(IOPATH A1 Z (r) (f))` twice), and Icarus 13.0 reports
# each as `SDF ERROR: ...: Unable to match ModPath A1 -> Z in <instance>`.
#
# Reproduced live against Icarus 13.0, which pinned down the mechanism the
# issue's write-up could only guess at: the mismatch is **not** an edge-
# qualifier mismatch between the SDF and the model. Icarus does not support
# `ifnone` on an edge-sensitive path at all -- it prints `sorry: ifnone with
# an edge-sensitive path is not supported.` at elaboration and *discards the
# path*, so the elaborated cell has no `A1 -> Z` modpath whatsoever. That
# rules out both rewrite directions the issue floated:
#
# - Rewriting the SDF entry to `(IOPATH (posedge A1) Z ...)` /
#   `(IOPATH (negedge A1) Z ...)` still fails (`Unable to match ModPath
#   posedge A1 -> Z`), because there is no modpath of either edge to match --
#   verified live. The same holds for direction 1 (emitting edge-qualified
#   entries from `klt place-and-route`): no SDF spelling can annotate a path
#   the simulator never elaborated.
# - Stripping `ifnone` from the *model* would make the paths elaborate, but
#   it would also make the annotation a timing assumption rather than a
#   transcription: the two SDF entries are the arc's two unateness senses
#   (output rise/fall when the arc acts as a buffer vs. as an inverter), and
#   one Icarus edge-sensitive modpath can carry only one (rise, fall) pair --
#   whichever entry landed last would silently time the other sense too.
#
# So this is a counted, honestly-reported drop (the issue's direction 3),
# with the same justification #2363's physical-only drop rests on: the
# entry's target modpath does not exist in the elaborated design, so there is
# nothing the entry could ever annotate, and removing it cannot change
# simulated timing relative to what Icarus does with it anyway. What the
# caller loses is real, though, and it is reported as such: those arcs run
# with **no** module-path delay at all (Icarus discarded the model's own
# placeholder delay along with the path), and `environment.sdf.dropped`
# names every dropped arc by cell type so a consumer can see exactly which
# cells were not timed.
#
# The exemption is decided structurally from the run's own cell models,
# never from the diagnostic text, and every ambiguity resolves toward *not*
# dropping:
#
# - only a bare `(IOPATH ...)` entry is eligible (a `COND`-wrapped one is
#   left alone);
# - only when the `CELLTYPE`'s model is found among the request's sources;
# - only when that arc has at least one `ifnone` edge-sensitive path **and no
#   other path Icarus keeps** (a plain, `if (...)`-conditional, or
#   non-`ifnone` edge-sensitive path all elaborate and annotate normally --
#   gf180mcu's MUX data inputs carry both, and are untouched);
# - never for a cell whose `specify` block contains a path statement this
#   module cannot parse (it might be the supported path that would have
#   matched).
#
# A genuine `IOPATH` mismatch onto any other arc -- a misnamed pin, a stale
# SDF -- is left in place and still fails the diagnostic gate loudly.
# --------------------------------------------------------------------------- #

#: A ``specify ... endspecify`` block's body, matched non-greedily.
_VERILOG_SPECIFY_BLOCK_RE = re.compile(r"\bspecify\b(.*?)\bendspecify\b", re.DOTALL)

#: A Verilog preprocessor directive line (`` `ifdef``/`` `else``/...), stripped
#: before a ``specify`` body is split into statements.
_VERILOG_DIRECTIVE_RE = re.compile(r"`[A-Za-z_]\w*[^\n]*")

#: A module-path connection operator: parallel ``=>`` or full ``*>``, each
#: optionally preceded by a ``+``/``-`` polarity.
_VERILOG_PATH_ARROW_RE = re.compile(r"[+-]?(?:=>|\*>)")

#: The edge keyword (and optional ``edge [01, 10]`` descriptor) that makes a
#: module path edge-sensitive.
_VERILOG_PATH_EDGE_RE = re.compile(r"\s*(?:posedge|negedge|edge)\b(?:\s*\[[^\]]*\])?")

#: A simple Verilog identifier -- the only port-name shape the arc matcher
#: compares (an escaped identifier is never matched, so never dropped).
_VERILOG_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

#: A bare ``IOPATH`` entry's two port specs: an optionally edge-qualified
#: input port and the output port.
_SDF_IOPATH_HEAD_RE = re.compile(
    r"\(IOPATH\s+(?:\(\s*(?:posedge|negedge)\s+(?P<edge_src>[^()\s]+)\s*\)"
    r"|(?P<src>[^()\s]+))\s+(?P<dst>[^()\s]+)"
)

#: An SDF ``(CELLTYPE "<name>")`` clause.
_SDF_CELLTYPE_RE = re.compile(r'\(CELLTYPE\s+"([^"]*)"\s*\)')

#: Sentinel :func:`_parse_specify_path` returns for a statement that carries
#: a path operator but could not be parsed -- distinct from ``None`` (not a
#: path statement at all), because an unparseable path disqualifies the whole
#: cell from the #2885 drop.
_UNPARSEABLE_PATH = "unparseable"


def _verilog_port_names(text: str) -> list[str]:
    """Every simple identifier in a comma-separated module-path terminal list
    (``A1``, ``A, B``, ``D[0]``), with any bit-select stripped. A terminal
    that is not a simple identifier is skipped, so it can never match."""
    names = []
    for token in text.split(","):
        name = re.sub(r"\[[^\]]*\]", "", token).strip()
        if _VERILOG_SIMPLE_IDENT_RE.match(name):
            names.append(name)
    return names


def _strip_specify_condition(statement: str) -> tuple[bool, str] | None:
    """``(is_ifnone, statement with its leading ifnone/if (...) removed)``,
    or ``None`` when an ``if (`` condition's parens do not balance."""
    ifnone = re.match(r"ifnone\b", statement)
    if ifnone is not None:
        return True, statement[ifnone.end() :].lstrip()
    if re.match(r"if\s*\(", statement):
        open_index = statement.index("(")
        try:
            close = _sdf_matching_paren(statement, open_index)
        except Exception:
            return None
        return False, statement[close + 1 :].lstrip()
    return False, statement


def _parse_specify_path(
    statement: str,
) -> tuple[bool, bool, list[str], list[str]] | str | None:
    """``(is_ifnone, is_edge_sensitive, sources, destinations)`` for one
    ``specify``-block statement that declares a module path; ``None`` for a
    statement that is not a path at all (a ``specparam``, a ``$setup`` timing
    check, ...); :data:`_UNPARSEABLE_PATH` for one that carries a path
    operator but could not be parsed."""
    stripped = _strip_specify_condition(statement.strip())
    if stripped is None:
        return _UNPARSEABLE_PATH
    ifnone, rest = stripped
    if not rest.startswith("("):
        return _UNPARSEABLE_PATH if _VERILOG_PATH_ARROW_RE.search(rest) else None
    try:
        body = rest[1 : _sdf_matching_paren(rest, 0)]
    except Exception:
        return _UNPARSEABLE_PATH
    arrow = _VERILOG_PATH_ARROW_RE.search(body)
    if arrow is None:
        return None
    left, right = body[: arrow.start()], body[arrow.end() :].strip()
    edge = _VERILOG_PATH_EDGE_RE.match(left)
    if edge is not None:
        left = left[edge.end() :]
    # An edge-sensitive path's destination is `(Z:A1)` / `(Q+:D)` -- the
    # output terminal(s) before the `:` data source, polarity dropped.
    right = right.strip("()").split(":")[0].rstrip("+- ")
    sources, destinations = _verilog_port_names(left), _verilog_port_names(right)
    if not sources or not destinations:
        return _UNPARSEABLE_PATH
    return ifnone, edge is not None, sources, destinations


def _verilog_cell_discarded_arcs(
    cell: str, source_texts: list[str]
) -> set[tuple[str, str]]:
    """Every ``(input, output)`` arc of Verilog module ``cell`` whose only
    module paths are ``ifnone`` + edge-sensitive -- the paths Icarus discards
    at elaboration (``sorry: ifnone with an edge-sensitive path is not
    supported``), leaving that arc with no modpath at all (issue #2885).

    Empty -- so nothing is dropped -- when no definition of ``cell`` is found,
    and when any path statement in any of its ``specify`` blocks cannot be
    parsed: every uncertainty resolves toward leaving the SDF entry in place,
    where it fails loudly rather than silently disappearing.
    """
    pattern = re.compile(
        _VERILOG_MODULE_CHUNK_TEMPLATE.format(name=re.escape(cell)), re.DOTALL
    )
    discarded: set[tuple[str, str]] = set()
    kept: set[tuple[str, str]] = set()
    for text in source_texts:
        for chunk_match in pattern.finditer(text):
            chunk = _VERILOG_COMMENT_RE.sub(" ", chunk_match.group(0))
            chunk = _VERILOG_DIRECTIVE_RE.sub(" ", chunk)
            for block in _VERILOG_SPECIFY_BLOCK_RE.findall(chunk):
                for statement in block.split(";"):
                    path = _parse_specify_path(statement)
                    if path == _UNPARSEABLE_PATH:
                        return set()
                    if path is None:
                        continue
                    ifnone, edge, sources, destinations = path  # type: ignore[misc, str-unpack]
                    target = discarded if ifnone and edge else kept
                    target.update((s, d) for s in sources for d in destinations)
    return discarded - kept


def _sdf_iopath_arc(entry_text: str) -> tuple[str, str] | None:
    """``(input port, output port)`` of a bare ``IOPATH`` entry (input edge
    qualifier and bit-select dropped), or ``None`` for any other entry --
    including a ``COND``-wrapped ``IOPATH``, which the #2885 drop never
    touches."""
    head = _SDF_IOPATH_HEAD_RE.match(entry_text.lstrip())
    if head is None:
        return None
    source = head.group("edge_src") or head.group("src")
    return (
        re.sub(r"\[[^\]]*\]", "", source),
        re.sub(r"\[[^\]]*\]", "", head.group("dst")),
    )


def _sdf_cell_celltype(text: str, cell_start: int) -> str | None:
    """The ``CELLTYPE`` of the ``(CELL ...)`` opening at ``cell_start``, or
    ``None`` when it has none (or the cell is malformed)."""
    try:
        cell_end = _sdf_matching_paren(text, cell_start) + 1
    except Exception:
        return None
    match = _SDF_CELLTYPE_RE.search(text, cell_start, cell_end)
    return match.group(1) if match else None


def _drop_sdf_unsupported_edge_modpath_iopaths(
    sdf_path: str, source_paths: list[str]
) -> tuple[str, int, list[dict[str, str]]] | None:
    """Remove every bare ``IOPATH`` entry in ``sdf_path`` onto a cell arc
    whose only module paths in the request's own cell models are ``ifnone``
    + edge-sensitive (issue #2885), returning ``(text with those entries
    removed, how many were removed, the distinct dropped arcs)`` -- or
    ``None`` when there is nothing to remove, in which case the caller keeps
    handing ``sdf_path`` itself to ``$sdf_annotate`` unchanged.

    Each dropped arc is ``{"celltype", "from", "to"}``, sorted, so the
    response can name exactly which cells' arcs run without a module-path
    delay. See the module-level comment above for why the entry is dropped
    rather than rewritten, and why that is decided from the elaborated model
    rather than from Icarus's diagnostic.

    Returns ``None`` immediately -- without reading the SDF -- when no source
    contains ``ifnone`` at all, the overwhelmingly common case (sky130's
    models, every behavioural RTL source).
    """
    source_texts = [
        text for text in _read_verilog_source_texts(source_paths) if "ifnone" in text
    ]
    if not source_texts:
        return None
    text = _read_sdf_text(sdf_path)
    if text is None or "IOPATH" not in text:
        return None

    discarded_by_cell: dict[str, set[tuple[str, str]]] = {}
    arcs: set[tuple[str, str, str]] = set()
    removed_spans: list[tuple[int, int]] = []
    dropped = 0
    for cell_match in re.finditer(r"\(CELL\b", text):
        celltype = _sdf_cell_celltype(text, cell_match.start())
        if celltype is None:
            continue
        if celltype not in discarded_by_cell:
            discarded_by_cell[celltype] = _verilog_cell_discarded_arcs(
                celltype, source_texts
            )
        discarded = discarded_by_cell[celltype]
        if not discarded:
            continue

        def _droppable(entry_text: str, _discarded=discarded, _cell=celltype) -> bool:
            arc = _sdf_iopath_arc(entry_text)
            if arc is None or arc not in _discarded:
                return False
            arcs.add((_cell, *arc))
            return True

        spans, count = _sdf_cell_entry_removals(text, cell_match.start(), _droppable)
        removed_spans.extend(spans)
        dropped += count

    if not dropped:
        return None
    return (
        _cut_sdf_spans(text, removed_spans),
        dropped,
        [{"celltype": c, "from": s, "to": d} for c, s, d in sorted(arcs)],
    )


# --------------------------------------------------------------------------- #
# Issue #2958: a bit-selected `IOPATH` onto a bus output of a hard macro.
#
# OpenSTA's `write_sdf` emits one `IOPATH` per bit of a macro's bus output
# (`IOPATH A_CLK A_DOUT[0] ...`), but a vendor macro model usually declares a
# single whole-vector module path (`(A_CLK *> A_DOUT)`). Icarus resolves an
# SDF `IOPATH` destination by exact terminal name against the modpaths it
# elaborated, and a whole-vector path registers no per-bit modpath: the
# unescaped spelling is an `SDF ERROR ... Invalid/malformed IOPATH` syntax
# error, the escaped one (`A_DOUT\[0\]`) `Unable to match ModPath` (both
# reproduced live against Icarus 13.0). No caller input can make those entries
# annotate, so -- like #2885's `ifnone` arcs -- they are dropped, counted, and
# the affected `{celltype, from, to}` arcs named.
#
# Rewriting the entry to the whole-vector spelling is rejected: it would
# attribute one bit's delay to the whole bus. Fail-safe: the drop only fires
# when the model declares the destination as a bus *and* every path onto it is
# whole-vector and parseable, and the entry's own arc is one of those paths.
# --------------------------------------------------------------------------- #

#: A bus ``output``/``inout`` declaration (ANSI or not): the keyword, optional
#: net/signedness keywords, a ``[msb:lsb]`` range, then the declared name(s).
_VERILOG_BUS_OUTPUT_RE = re.compile(
    r"\b(?:output|inout)\s+(?:(?:reg|wire|logic|signed)\s+)*\[[^\]]*\]\s*"
    r"([A-Za-z_][A-Za-z0-9_$]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_$]*)*)"
)

#: Words that can follow a comma in an ANSI port list without being a name.
_VERILOG_DECL_KEYWORDS = frozenset(
    {"input", "output", "inout", "reg", "wire", "logic", "signed"}
)

#: A bit-select suffix on an SDF port, plain (``[0]``) or escaped (``\\[0\\]``).
_SDF_BIT_SELECT_RE = re.compile(r"\\?\[")


def _verilog_bus_output_names(chunk: str) -> set[str]:
    """Names declared as bus ``output``/``inout`` ports in a module chunk."""
    names: set[str] = set()
    for match in _VERILOG_BUS_OUTPUT_RE.finditer(chunk):
        for name in re.split(r"\s*,\s*", match.group(1)):
            if name not in _VERILOG_DECL_KEYWORDS:
                names.add(name)
    return names


def _verilog_cell_whole_vector_arcs(
    cell: str, source_texts: list[str]
) -> set[tuple[str, str]]:
    """Every ``(input, output)`` arc of Verilog module ``cell`` whose output
    is a declared bus and whose module paths onto it are all whole-vector
    (no bit-select on the destination terminal) -- the arcs a per-bit SDF
    ``IOPATH`` can never match (issue #2958).

    Empty -- so nothing is dropped -- when no definition of ``cell`` is found,
    when any path statement cannot be parsed, or for an output that has any
    bit-indexed module path (its per-bit entries may then match).
    """
    pattern = re.compile(
        _VERILOG_MODULE_CHUNK_TEMPLATE.format(name=re.escape(cell)), re.DOTALL
    )
    buses: set[str] = set()
    whole: set[tuple[str, str]] = set()
    indexed: set[str] = set()
    for text in source_texts:
        for chunk_match in pattern.finditer(text):
            chunk = _VERILOG_COMMENT_RE.sub(" ", chunk_match.group(0))
            chunk = _VERILOG_DIRECTIVE_RE.sub(" ", chunk)
            buses |= _verilog_bus_output_names(chunk)
            for block in _VERILOG_SPECIFY_BLOCK_RE.findall(chunk):
                for statement in block.split(";"):
                    path = _parse_specify_path(statement)
                    if path == _UNPARSEABLE_PATH:
                        return set()
                    if path is None:
                        continue
                    _, _, sources, destinations = path  # type: ignore[misc, str-unpack]
                    raw = _strip_specify_condition(statement.strip())
                    if raw is not None and re.search(r"(?:=>|\*>)[^;]*\[", raw[1]):
                        indexed.update(destinations)
                    whole.update((s, d) for s in sources for d in destinations)
    return {(s, d) for s, d in whole if d in buses and d not in indexed}


def _sdf_bit_selected_iopath_arc(entry_text: str) -> tuple[str, str] | None:
    """``(input port, output bus)`` of a bare ``IOPATH`` entry whose output
    carries a bit-select (plain or escaped), or ``None`` for any other entry."""
    head = _SDF_IOPATH_HEAD_RE.match(entry_text.lstrip())
    if head is None:
        return None
    destination = head.group("dst")
    base = _SDF_BIT_SELECT_RE.split(destination, maxsplit=1)[0]
    if base == destination:
        return None
    return head.group("edge_src") or head.group("src"), base


def _drop_sdf_bit_selected_bus_iopaths(
    sdf_path: str, source_paths: list[str]
) -> tuple[str, int, list[dict[str, str]]] | None:
    """Remove every bare ``IOPATH`` entry in ``sdf_path`` whose bit-selected
    output is a bus the cell model reaches only through a whole-vector module
    path (issue #2958), returning ``(text with those entries removed, how many
    were removed, the distinct dropped arcs)`` -- or ``None`` when there is
    nothing to remove. Each arc is ``{"celltype", "from", "to"}`` (``to`` is
    the bus name, bit-select dropped), sorted.

    Returns ``None`` immediately -- without reading the SDF -- when no source
    declares a bus output next to a ``specify`` block.
    """
    source_texts = [
        text
        for text in _read_verilog_source_texts(source_paths)
        if "specify" in text and _VERILOG_BUS_OUTPUT_RE.search(text)
    ]
    if not source_texts:
        return None
    text = _read_sdf_text(sdf_path)
    if text is None or "IOPATH" not in text or "[" not in text:
        return None

    arcs_by_cell: dict[str, set[tuple[str, str]]] = {}
    arcs: set[tuple[str, str, str]] = set()
    removed_spans: list[tuple[int, int]] = []
    dropped = 0
    for cell_match in re.finditer(r"\(CELL\b", text):
        celltype = _sdf_cell_celltype(text, cell_match.start())
        if celltype is None:
            continue
        if celltype not in arcs_by_cell:
            arcs_by_cell[celltype] = _verilog_cell_whole_vector_arcs(
                celltype, source_texts
            )
        whole = arcs_by_cell[celltype]
        if not whole:
            continue

        def _droppable(entry_text: str, _whole=whole, _cell=celltype) -> bool:
            arc = _sdf_bit_selected_iopath_arc(entry_text)
            if arc is None or arc not in _whole:
                return False
            arcs.add((_cell, *arc))
            return True

        spans, count = _sdf_cell_entry_removals(text, cell_match.start(), _droppable)
        removed_spans.extend(spans)
        dropped += count

    if not dropped:
        return None
    return (
        _cut_sdf_spans(text, removed_spans),
        dropped,
        [{"celltype": c, "from": s, "to": d} for c, s, d in sorted(arcs)],
    )


# --------------------------------------------------------------------------- #
# Issue #1890: an `INTERCONNECT` entry whose endpoint is an escaped Verilog
# identifier containing a literal occurrence of the SDF file's own `DIVIDER`
# character (e.g. `g\[0\]\.sub\/x.a` with `DIVIDER .` -- exactly what a
# `generate`-block array of sub-modules becomes once synthesis/place-and-
# route flattens it: Yosys `flatten`, OpenROAD's post-route
# `write_verilog`/`write_sdf`) crashes `vvp` outright: `ERROR: NULL handle
# passed to vpi_scan.` / an `Assertion `0' failed` SIGABRT, confirmed live
# against Icarus 13.0 (stable).
#
# Root-caused (live bisection against raw `iverilog`/`vvp`, not just through
# this module) to Icarus's own `INTERCONNECT` path splitter: it tokenizes an
# endpoint on every literal occurrence of the DIVIDER character, without
# honoring a preceding backslash-escape that (per the same escaped-identifier
# convention Verilog itself uses) marks that occurrence as *data*, not a
# hierarchy separator. That produces one extra, nonexistent intermediate
# scope reference, and the VPI resolution code that walks it calls
# `vpi_scan` on the resulting NULL handle without checking for NULL first --
# an assertion failure the caller cannot catch, only avoid triggering.
#
# This is independent of hierarchy depth, of the top-level-port wrapper
# (issue #1056), of `-ginterconnect`, and of which of the two DIVIDER
# characters (`.` or `/`) is in play -- verified by isolating each variable
# in turn: an escaped occurrence of the character the file's own `(DIVIDER
# ...)` header declares reproduces the crash; an escaped occurrence of the
# *other* legal divider character, or an escaped bracket alone with no
# embedded divider character, does not. There is therefore no generated
# shim/wrapper shape (unlike #1056's nested-DUT wrapper, which worked around
# a *scope-resolution* failure) that can dodge this: the crash is entirely a
# function of the SDF file's own `INTERCONNECT` text, decided before
# `$sdf_annotate` ever sees a scope argument. This is a genuine Icarus
# upstream limitation -- guarded here with a clear, request-validation-time
# error instead of a bare `vvp` assertion abort/core dump.
#
# Issue #2897 follow-up (re-verified live, Icarus 13.0): an escaped `.` is
# mis-split under *either* DIVIDER, so canonicalizing the header to `/` does
# not help (still aborts); renaming the escaped *instance* to a plain
# identifier in build-dir copies of the SDF and netlist does. That rename
# lives in `functional_verification_sdf_rename.py` and runs first, so the
# guard below now only fires for the residual pin/top-level-port shape.
# --------------------------------------------------------------------------- #


def _reject_sdf_escaped_divider_interconnects(sdf_path: str) -> None:
    """Fail loud, before any build artifact is written, when ``sdf_path``
    has an ``INTERCONNECT`` endpoint whose *pin or top-level port* (the
    endpoint's final path component) is an escaped identifier containing a
    literal ``.`` or the file's own ``DIVIDER`` character -- the shape that
    crashes Icarus's ``$sdf_annotate`` outright (issue #1890; see the
    module comment above for the confirmed root cause).

    Narrowed by issue #2897: the same shape in an *instance* component (a
    flattened ``generate`` array's ``g\\[0\\]\\.sub\\.u1``, the common case)
    is no longer rejected -- ``functional_verification_sdf_rename``'s
    ``_rename_sdf_escaped_divider_instances`` renames it to a plain
    identifier in build-dir copies of the SDF and netlist instead. A
    pin/port is never renamed (the testbench reaches top-level ports by
    name), so only that residual shape still lands here.

    Never raises on a malformed/unreadable SDF file -- that is the
    simulator's own parser's job to report.
    """
    from .functional_verification import FunctionalVerificationError

    try:
        with open(sdf_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return

    offenders = _sdf_unresolvable_pin_tokens(text)
    if not offenders:
        return

    examples = ", ".join(sorted(set(offenders))[:3])
    raise FunctionalVerificationError(
        "options.sdf: this SDF has an INTERCONNECT entry whose pin or "
        "top-level port is an escaped identifier containing a literal '.' "
        f"or the file's own DIVIDER character (e.g. {examples}). This is a "
        "known Icarus Verilog upstream limitation: Icarus's INTERCONNECT "
        "path splitter does not honor the backslash-escape on that "
        "character, which crashes vvp outright ('ERROR: NULL handle passed "
        "to vpi_scan.', SIGABRT). klt renames an *instance* name of this "
        "shape to a plain identifier automatically (issue #2897), but not a "
        "pin or top-level port name, which the testbench addresses directly "
        "-- rename that port in the netlist and SDF to a plain identifier"
    )


def _check_sdf_engine_capability(version: str | None) -> None:
    """Reject an ``options.sdf`` request the resolved Icarus cannot serve
    (issue #1004's finding, folded into #1002's own implementation).

    ``-ginterconnect`` -- mandatory for a post-route SDF, whose entire
    net-delay content is ``INTERCONNECT`` entries -- does not exist before
    Icarus 13.0. On 12.0 ``iverilog`` fails the *build* with ``Unknown/
    Unsupported Language generation interconnect`` and exit 255, four layers
    below this API, which reads as "the simulator is broken" rather than
    "this host's Icarus is too old for SDF". Probing
    :data:`SDF_MIN_ICARUS_MAJOR` up front converts that into a request error
    naming the actual constraint.

    An **unresolvable** version (``None`` -- ``iverilog -V`` missing or
    unparsable) is deliberately *not* an error: the probe is a courtesy, not
    a gate, and a failed build plus the transcript scan still catch a real
    incompatibility. Refusing to run on an unreadable version string would
    trade a clear downstream failure for a spurious upstream one.
    """
    from .functional_verification import (
        SDF_MIN_ICARUS_MAJOR,
        FunctionalVerificationError,
    )

    if version is None:
        return
    match = re.match(r"(\d+)", version)
    if match is None:
        return
    if int(match.group(1)) >= SDF_MIN_ICARUS_MAJOR:
        return
    raise FunctionalVerificationError(
        f"options.sdf requires Icarus Verilog {SDF_MIN_ICARUS_MAJOR}.0 or "
        f"newer -- the resolved iverilog is version {version}, which has no "
        "'-ginterconnect' flag ('Unknown/Unsupported Language generation "
        "interconnect'). A post-route SDF's net delays are INTERCONNECT "
        "entries, so they cannot be annotated on this build at all"
    )


def _scan_sdf_diagnostics(*log_paths: str) -> tuple[list[str], dict[str, int]]:
    """Every *actionable* SDF diagnostic Icarus emitted across ``log_paths``,
    plus a per-class count of the *benign* diagnostics filtered out alongside
    them.

    Non-empty ``actionable`` means the annotation did not fully apply --
    regardless of the run's own pass/fail verdict, which is exactly the trap:
    `vvp` exits ``0`` in every SDF failure mode (spike §3.3).

    The second element counts diagnostics that matched
    :data:`SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS` and were therefore excluded from
    ``actionable`` -- a real ``write_sdf`` emits ``TIMINGCHECK`` sections
    Icarus does not implement, and a gate that rejected them would reject
    every real post-route SDF. Counting them, rather than silently discarding
    them the way this function did before issue #1102, is what lets a caller
    distinguish a scan with no observed benign diagnostic from one that
    observed dropped TIMINGCHECKs -- both otherwise report ``annotated: true``
    identically. These counts do not establish complete annotation coverage.

    A marker-bearing line is classified at all only once it matches
    :data:`SDF_DIAGNOSTIC_LINE_RE` -- the ``SDF WARNING:``/``SDF ERROR:``
    ``<file>:<line>:`` shape Icarus gives every diagnostic it writes. One
    that carries the marker but not the shape is a *corrupted* line, not a
    diagnostic (issue #1136): Icarus's own C-level ``printf`` output and
    cocotb's Python logging share one stdout fd, and under a non-tty capture
    both are commonly fully- rather than line-buffered, so a flush from one
    can land mid-line inside a not-yet-flushed line from the other. The
    observed splice ate the tail of a benign ``TIMINGCHECK`` warning -- and
    with it the very substring the benign exemption keys on -- turning a
    fully-applied annotation into a reported failure.

    Such a line is dropped from *both* buckets rather than counted in
    either: nothing after the marker survived the splice, so it is no more
    evidence of a real problem than of a benign one. That cannot mask a real
    failure in practice, because Icarus emits one diagnostic **per failing
    SDF entry** -- the failure modes this gate exists to catch arrive in the
    dozens-to-hundreds (spike §3.3's 50-flop netlist annotated 0 flops and
    logged 50 ``SDF ERROR`` lines), while a splice corrupts only the single
    line an interleaved flush lands in, leaving every sibling intact and the
    gate firing on those.

    The same applies to a line that matches the shape but also contains an
    embedded cocotb log-line header (:data:`SDF_SPLICED_LOG_HEADER_RE`,
    issue #2996): the splice landed after the locator -- inside the message,
    or inside the path with the spliced-in line's own locator -- so the
    message is cut (``TIM`` rather than ``TIMINGCHECK``) and equally
    untrustworthy. Its orphaned tail (``INGCHECK not supported.``) carries no
    marker and is already ignored.

    Every supplied transcript must be successfully read. Missing, unreadable,
    or incompletely read logs raise ``FunctionalVerificationError``: this scan
    gates affirmative annotation credit, so unavailable observation cannot be
    treated as an observed clean result (issue #2130). An empty, successfully
    read log contains no observed diagnostic; it is not proof of complete
    annotation coverage.
    """
    from .functional_verification import (
        SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS,
        SDF_DIAGNOSTIC_LINE_RE,
        SDF_DIAGNOSTIC_MARKERS,
        SDF_OMITTED_ANNOTATION_MARKER,
        SDF_SPLICED_LOG_HEADER_RE,
        FunctionalVerificationError,
    )

    actionable: list[str] = []
    dropped: dict[str, int] = {}
    for log_path in log_paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            raise FunctionalVerificationError(
                f"could not read required SDF annotation transcript '{log_path}': "
                f"{exc} -- annotation diagnostics are unavailable; this run "
                "cannot qualify as SDF-annotated evidence"
            ) from exc
        for line in lines:
            stripped = line.strip()
            if SDF_OMITTED_ANNOTATION_MARKER in stripped:
                actionable.append(stripped)
                continue
            if not any(marker in stripped for marker in SDF_DIAGNOSTIC_MARKERS):
                continue
            if not SDF_DIAGNOSTIC_LINE_RE.match(stripped):
                # Marker present, Icarus's own shape absent: a stdout splice,
                # not a diagnostic (issue #1136). Untrustworthy in both
                # directions, so it is counted in neither.
                continue
            if SDF_SPLICED_LOG_HEADER_RE.search(stripped):
                # Well-formed head, but a cocotb log header is embedded in
                # it: the same splice landing after the locator (issue
                # #2996). Dropped from both buckets, as above.
                continue
            benign_class = next(
                (
                    benign
                    for benign in SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS
                    if benign in stripped
                ),
                None,
            )
            if benign_class is not None:
                key = benign_class.lower()
                dropped[key] = dropped.get(key, 0) + 1
                continue
            actionable.append(stripped)
    return actionable, dropped
