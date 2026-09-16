"""SDF (Standard Delay Format) timing-annotation subsystem for the cocotb
functional-verification flow.

Split out of ``functional_verification.py`` (issue #1909) as a
self-contained, low-coupling subsystem: option validation
(:func:`_reject_sdf_with_functional_models`, :func:`_resolve_sdf_option`),
elaboration-root generation (:func:`_write_sdf_annotate_shim`,
:func:`_parse_toplevel_ports`, :func:`_write_sdf_dut_wrapper`), SDF-text
paren/clause scanning and bit-selected top-level-port ``INTERCONNECT``
deferral (:func:`_sdf_matching_paren`, :func:`_sdf_top_level_clauses`,
:func:`_split_sdf_bus_port_interconnects`), and post-run diagnostics
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
from typing import Any


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
    return ``{"file": <abs path>, "corner": ...}``, or ``None`` when absent.

    Unknown keys are rejected rather than ignored. The block's whole surface
    is two fields, and both of the plausible typos (``"corners"``,
    ``"path"``) would otherwise degrade to a *silently different run* -- the
    wrong timing corner, or no annotation at all -- which is precisely the
    class of failure this feature's own transcript gate exists to prevent.
    """
    from .functional_verification import (
        DEFAULT_SDF_CORNER,
        SDF_CORNERS,
        SDF_ENGINES,
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

    unknown = sorted(set(sdf) - {"file", "corner"})
    if unknown:
        raise FunctionalVerificationError(
            "request.options.sdf has unknown field(s): "
            + ", ".join(unknown)
            + " (supported: file, corner)"
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

    return {"file": os.path.abspath(path), "corner": corner}


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
    tell "every check in the SDF was applied" apart from "every delay was
    applied and every TIMINGCHECK was dropped" -- both of which otherwise
    report ``annotated: true`` identically.

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

    Never raises: a missing/unreadable transcript contributes nothing, the
    same posture :func:`_log_tail` takes.
    """
    from .functional_verification import (
        SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS,
        SDF_DIAGNOSTIC_LINE_RE,
        SDF_DIAGNOSTIC_MARKERS,
        SDF_OMITTED_ANNOTATION_MARKER,
    )

    actionable: list[str] = []
    dropped: dict[str, int] = {}
    for log_path in log_paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
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
