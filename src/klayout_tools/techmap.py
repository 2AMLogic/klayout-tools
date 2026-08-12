"""The technology-mapping stage's acceptance gate: a mapped gate-level
netlist is accepted only when ``klt equiv`` proves it logically equivalent
to the pre-mapping generic netlist it was produced from.

This is Phase 2c of the RTL-synthesis epic
([#704](https://github.com/2AMLogic/klayout-tools/issues/704)), issue #875.
Phase 1b (#808) wired ``klt equiv`` (#707/#726) in as ``klt synthesize``'s
own acceptance gate between **RTL** and the synthesized netlist; Phase 2b
(#874, ``native/techmap/``) added a second stage where correctness can
silently break -- technology mapping, from a
``klt.synth.generic-netlist/1`` document
(``docs/schemas/synth-generic-netlist.schema.json``) to a mapped
standard-cell netlist. This module extends the same gate one stage later,
between the **pre-mapping generic netlist** and the mapped netlist, per the
epic's "no claim without a runnable check" discipline.

Pure library: every function returns plain Python data and never prints,
mirroring ``synthesize.py``/``equiv.py``.

## How the gate works

``klt equiv`` compares two *Verilog* designs (``equiv.py``'s ``gold``/
``gate`` request sides), but this stage's own input is JSON, not Verilog.
So the gate:

1. Renders the generic netlist to behavioural Verilog
   (:func:`generic_netlist_to_verilog`) -- one ``assign`` per generic cell,
   using the exact function each of the contract's technology-independent
   primitives is defined by
   (``docs/design/synth-techmap-stage-contract.md`` section 2.2). The
   rendered module's port declarations mirror ``native/techmap``'s own
   ``verilog.rs`` (``port_decl``) bit for bit, so both sides of the miter
   present the same interface and no ``port_map`` is needed.
2. Runs :func:`klayout_tools.equiv.run_equiv` with that rendering as
   ``gold`` and the mapped netlist as ``gate`` (with ``liberty`` attached,
   so the standard-cell instances resolve as real logic rather than
   undefined blackboxes -- see :func:`klayout_tools.equiv._resolve_side`).
3. Raises :class:`TechmapError` for anything other than an
   ``"equivalent"`` verdict -- a proven ``"counterexample"`` (the mapper
   changed the logic) *and* an ``"inconclusive"`` one (a solver/process
   timeout is never a pass). A hard failure, never a silent warning:
   exactly the posture ``synthesize.py``'s own
   ``_verify_synthesis_equivalence`` already takes one stage earlier.

## Scope: combinational designs only

``klt equiv``'s Phase 0 MVP is combinational-only (``docs/cli/equiv.md``,
"Scope"), so a generic netlist containing the contract's ``$dff``
primitive cannot be gated today: :func:`generic_netlist_to_verilog`
rejects it up front with a clear scope error naming #707, rather than
emitting sequential Verilog only for Yosys to reject it later with a less
actionable message. This is the same limitation ``klt synthesize
--verify-equivalence`` already carries at the RTL stage -- stated plainly
here rather than papered over.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

from .equiv import EquivError, run_equiv

#: Bumped only on a non-additive (breaking) change to this module's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: The generic-netlist schema this gate understands
#: (``docs/schemas/synth-generic-netlist.schema.json``).
GENERIC_NETLIST_SCHEMA = "klt.synth.generic-netlist/1"

#: The standalone technology-mapping binary ``native/techmap/`` builds
#: (``cargo build --release``), invoked by :func:`run_techmap`.
TECHMAP_BINARY_NAME = "klt-techmap"

#: Environment variable overriding :func:`_resolve_binary`'s search.
TECHMAP_BINARY_ENV = "KLT_TECHMAP_BIN"

#: The combinational primitives of the contract's generic cell vocabulary
#: (``docs/design/synth-techmap-stage-contract.md`` section 2.2), mapped to
#: ``(ordered input pins, output pin, expression template)``. ``$dff`` --
#: the vocabulary's tenth, sequential primitive -- is deliberately absent;
#: see this module's docstring, "Scope".
_COMBINATIONAL_CELLS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "$not": (("A",), "Y", "~{A}"),
    "$buf": (("A",), "Y", "{A}"),
    "$and2": (("A", "B"), "Y", "{A} & {B}"),
    "$nand2": (("A", "B"), "Y", "~({A} & {B})"),
    "$or2": (("A", "B"), "Y", "{A} | {B}"),
    "$nor2": (("A", "B"), "Y", "~({A} | {B})"),
    "$xor2": (("A", "B"), "Y", "{A} ^ {B}"),
    "$xnor2": (("A", "B"), "Y", "~({A} ^ {B})"),
    # `Y = S ? B : A` -- the contract's own definition, not the opposite
    # polarity some libraries use.
    "$mux2": (("A", "B", "S"), "Y", "{S} ? {B} : {A}"),
}

#: The contract's reserved constant-net names (section 8) and the Verilog
#: literal each stands for. ``native/techmap``'s ``map.rs`` resolves the
#: same two names to a real tie cell on the mapped side.
_CONSTANT_NETS = {"$const0": "1'b0", "$const1": "1'b1"}

#: A net name usable verbatim as a Verilog identifier; anything else is
#: emitted as an escaped identifier (see :func:`_escape_identifier`).
_PLAIN_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class TechmapError(Exception):
    """Raised when technology mapping cannot be completed *or accepted*:
    a malformed generic netlist, a generic cell outside the contract's
    vocabulary, a design outside ``klt equiv``'s combinational-only scope,
    a missing/failed ``klt-techmap`` binary, or -- the gate's whole point
    -- a mapped netlist ``klt equiv`` could not prove equivalent to its own
    pre-mapping generic netlist.
    """


def load_generic_netlist(path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt.synth.generic-netlist/1``
    document, raising :class:`TechmapError` for a missing/unreadable file,
    invalid JSON, a foreign ``schema``, or a missing/mis-typed
    ``top``/``ports``/``cells`` field."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise TechmapError(f"could not read generic netlist '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TechmapError(
            f"generic netlist '{path}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise TechmapError(f"generic netlist '{path}' must be a JSON object")

    schema = document.get("schema")
    if schema != GENERIC_NETLIST_SCHEMA:
        raise TechmapError(
            f"generic netlist '{path}' declares schema {schema!r} "
            f"(expected '{GENERIC_NETLIST_SCHEMA}')"
        )

    top = document.get("top")
    if not isinstance(top, str) or not top:
        raise TechmapError(
            f"generic netlist '{path}': 'top' is required and must be a "
            "non-empty string"
        )
    for field in ("ports", "cells"):
        if not isinstance(document.get(field), list):
            raise TechmapError(f"generic netlist '{path}': '{field}' must be an array")
    return document


def generic_netlist_to_verilog(netlist: dict[str, Any]) -> str:
    """Render a ``klt.synth.generic-netlist/1`` document as behavioural
    Verilog -- the ``gold`` side of this stage's own equivalence gate.

    One ``assign`` per generic cell, using the exact Boolean function
    ``docs/design/synth-techmap-stage-contract.md`` section 2.2 defines for
    each primitive. Port declarations follow ``native/techmap``'s own
    ``verilog.rs::port_decl`` rule exactly (a 1-bit port whose single bit
    net *is* the port name declares as a scalar; anything else declares as
    ``[len-1:0]``), so the rendered module and the mapped netlist present
    an identical interface to ``miter -equiv``.

    Raises :class:`TechmapError` for a malformed document, a cell type
    outside the contract's vocabulary, a cell missing one of its
    primitive's pins, or -- see this module's docstring, "Scope" -- a
    ``$dff``, which is outside ``klt equiv``'s combinational-only MVP.
    """
    top = netlist.get("top")
    if not isinstance(top, str) or not top:
        raise TechmapError("generic netlist: 'top' must be a non-empty string")

    ports = netlist.get("ports")
    if not isinstance(ports, list):
        raise TechmapError("generic netlist: 'ports' must be an array")
    cells = netlist.get("cells")
    if not isinstance(cells, list):
        raise TechmapError("generic netlist: 'cells' must be an array")

    port_names: list[str] = []
    port_decls: list[str] = []
    port_bits: set[str] = set()
    for port in ports:
        if not isinstance(port, dict):
            raise TechmapError("generic netlist: each 'ports' entry must be an object")
        name = port.get("name")
        direction = port.get("direction")
        bits = port.get("bits")
        if not isinstance(name, str) or not name:
            raise TechmapError("generic netlist: a port has no 'name'")
        if direction not in ("input", "output", "inout"):
            raise TechmapError(
                f"generic netlist: port '{name}' has direction {direction!r} "
                "(expected 'input', 'output', or 'inout')"
            )
        if not isinstance(bits, list) or not bits:
            raise TechmapError(
                f"generic netlist: port '{name}' must declare a non-empty 'bits' array"
            )
        if not all(isinstance(bit, str) and bit for bit in bits):
            raise TechmapError(
                f"generic netlist: port '{name}' has a non-string 'bits' entry"
            )
        port_names.append(name)
        if len(bits) == 1 and bits[0] == name:
            port_decls.append(f"  {direction} {name};")
        else:
            # A compact `[msb:0]` declaration is only a faithful rendering
            # if the port's bit nets really are that bus's own bits, in
            # MSB-first order -- the dense, zero-indexed convention the
            # contract's section 2.3 documents and `native/techmap`'s own
            # `verilog.rs::port_decl` already assumes on the mapped side.
            # Reject a port that breaks it here, with a message naming the
            # offending bit, rather than silently emitting Verilog whose
            # bit references point somewhere else than the caller meant.
            expected = [f"{name}[{index}]" for index in range(len(bits) - 1, -1, -1)]
            if bits != expected:
                raise TechmapError(
                    f"generic netlist: port '{name}' declares bits {bits!r}, "
                    f"but this gate (like native/techmap's own emitter) "
                    f"requires the dense MSB-first convention {expected!r} "
                    "(docs/design/synth-techmap-stage-contract.md section 2.3)"
                )
            port_decls.append(f"  {direction} [{len(bits) - 1}:0] {name};")
        port_bits.update(bits)

    assignments: list[str] = []
    internal_nets: set[str] = set()

    def reference(net: Any, cell_name: str, pin: str) -> str:
        if not isinstance(net, str) or not net:
            raise TechmapError(
                f"generic netlist: cell '{cell_name}' pin '{pin}' is not "
                "connected to a net name"
            )
        if net in _CONSTANT_NETS:
            return _CONSTANT_NETS[net]
        if net in port_bits:
            # Emitted verbatim -- a port bit's own net name is already a
            # legal reference into the port declared above (`a[0]`, `cin`),
            # the same assumption `native/techmap`'s verilog.rs makes.
            return net
        internal_nets.add(net)
        return _escape_identifier(net)

    for cell in cells:
        if not isinstance(cell, dict):
            raise TechmapError("generic netlist: each 'cells' entry must be an object")
        cell_name = cell.get("name")
        cell_type = cell.get("type")
        connections = cell.get("connections")
        if not isinstance(cell_name, str) or not cell_name:
            raise TechmapError("generic netlist: a cell has no 'name'")
        if not isinstance(connections, dict):
            raise TechmapError(
                f"generic netlist: cell '{cell_name}' has no 'connections' object"
            )
        if cell_type == "$dff":
            raise TechmapError(
                f"generic netlist cell '{cell_name}' is a '$dff': the "
                "technology-mapping equivalence gate is combinational-only, "
                "because klt equiv's Phase 0 MVP (issue #707) is -- a design "
                "with flip-flops, latches, or memories cannot be gated until "
                "a sequential equivalence engine lands (see docs/cli/equiv.md, "
                "'Scope')"
            )
        spec = _COMBINATIONAL_CELLS.get(cell_type)
        if spec is None:
            raise TechmapError(
                f"generic netlist: cell '{cell_name}' has type {cell_type!r}, "
                "which is outside the generic cell vocabulary "
                "(docs/design/synth-techmap-stage-contract.md section 2.2): "
                + ", ".join(sorted(_COMBINATIONAL_CELLS) + ["$dff"])
            )
        input_pins, output_pin, template = spec
        for pin in (*input_pins, output_pin):
            if pin not in connections:
                raise TechmapError(
                    f"generic netlist: cell '{cell_name}' ({cell_type}) is "
                    f"missing its '{pin}' pin connection"
                )
        expression = template.format(
            **{pin: reference(connections[pin], cell_name, pin) for pin in input_pins}
        )
        target = reference(connections[output_pin], cell_name, output_pin)
        assignments.append(f"  assign {target} = {expression};")

    lines = [
        "/* Generated by klt's technology-mapping equivalence gate (issue #875) -- */",
        "/* the pre-mapping generic netlist rendered as behavioural Verilog, */",
        "/* the `gold` side of the klt equiv proof against the mapped netlist. */",
        "",
        f"module {top} ({', '.join(port_names)});",
        *port_decls,
    ]
    for net in sorted(internal_nets - port_bits):
        lines.append(f"  wire {_escape_identifier(net)};")
    lines.append("")
    lines.extend(assignments)
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _escape_identifier(net: str) -> str:
    """Return ``net`` as a legal Verilog identifier reference.

    A name that is already a plain identifier is returned unchanged;
    anything else (a bus-bit name like ``n[3]`` that is *not* a declared
    port bit, a name with a ``$`` or ``.`` in it) becomes a Verilog escaped
    identifier -- a leading backslash and a mandatory trailing space, the
    same form Yosys's own ``write_verilog`` emits.
    """
    if _PLAIN_IDENTIFIER_RE.fullmatch(net):
        return net
    return f"\\{net} "


def verify_mapping_equivalence(
    *,
    generic_netlist_path: str,
    mapped_netlist_path: str,
    liberty_path: str,
    output_dir: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Prove the mapped netlist at ``mapped_netlist_path`` logically
    equivalent to the pre-mapping generic netlist at
    ``generic_netlist_path`` -- the acceptance gate itself.

    ``liberty_path`` is the same resolved ``.lib`` the mapping run used; it
    is attached to the ``gate`` side so the standard-cell instances resolve
    as real logic rather than undefined blackboxes.

    Artifacts (the rendered ``gold`` Verilog, the generated ``klt equiv``
    request, and -- via ``run_equiv`` -- its own script/netlist/log) are
    written under ``output_dir``, defaulting to ``.klt/techmap/`` next to
    the generic netlist. The equiv request is written as a real *file*
    (never passed inline) so ``run_equiv`` anchors its own
    ``.klt/equiv/`` artifacts there too rather than in the process's
    current working directory.

    Returns a summary dict on an ``"equivalent"`` verdict. Raises
    :class:`TechmapError` for every other outcome -- a proven
    ``"counterexample"``, an ``"inconclusive"`` verdict (solver/process
    timeout, never treated as a pass), or an
    :class:`~klayout_tools.equiv.EquivError`.
    """
    netlist = load_generic_netlist(generic_netlist_path)
    top = netlist["top"]

    if not os.path.isfile(mapped_netlist_path):
        raise TechmapError(f"mapped netlist not found: {mapped_netlist_path}")
    if not os.path.isfile(liberty_path):
        raise TechmapError(f"liberty file not found: {liberty_path}")

    if output_dir is None:
        output_dir = os.path.join(
            os.path.dirname(os.path.abspath(generic_netlist_path)), ".klt", "techmap"
        )
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise TechmapError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    gold_path = os.path.join(output_dir, f"{top}_generic.v")
    try:
        with open(gold_path, "w", encoding="utf-8") as handle:
            handle.write(generic_netlist_to_verilog(netlist))
    except OSError as exc:
        raise TechmapError(
            f"could not write generic-netlist Verilog '{gold_path}': {exc}"
        ) from exc

    equiv_request_path = os.path.join(output_dir, f"equiv_request_{top}.json")
    equiv_request = {
        "gold": {"sources": [gold_path], "top": top},
        "gate": {
            "sources": [os.path.abspath(mapped_netlist_path)],
            "top": top,
            "liberty": os.path.abspath(liberty_path),
        },
    }
    try:
        with open(equiv_request_path, "w", encoding="utf-8") as handle:
            json.dump(equiv_request, handle, indent=2)
    except OSError as exc:
        raise TechmapError(
            f"could not write equivalence-check request '{equiv_request_path}': {exc}"
        ) from exc

    try:
        equiv_report = run_equiv(equiv_request_path, timeout_s=timeout_s)
    except EquivError as exc:
        raise TechmapError(
            "equivalence check against the pre-mapping generic netlist could "
            f"not be completed: {exc}"
        ) from exc

    status = equiv_report["status"]
    if status != "equivalent":
        raise TechmapError(_equivalence_failure_message(status, equiv_report))

    return {
        "status": status,
        "engine": equiv_report["engine"],
        "engine_version": equiv_report["engine_version"],
        "timeout_s": equiv_report["timeout_s"],
        "elapsed_s": equiv_report["elapsed_s"],
        "generic_netlist_verilog_path": gold_path,
        "artifacts": equiv_report["artifacts"],
    }


def _equivalence_failure_message(status: str, equiv_report: dict[str, Any]) -> str:
    """An actionable :class:`TechmapError` message for a non-
    ``"equivalent"`` verdict -- names the diverging outputs for a proven
    ``"counterexample"``, or the first diagnostic for an ``"inconclusive"``
    (timeout) one. Mirrors ``synthesize.py``'s own equivalent helper at the
    RTL stage."""
    log_path = equiv_report.get("artifacts", {}).get("log_path")
    if status == "counterexample":
        counterexample = equiv_report.get("counterexample") or {}
        diverging = counterexample.get("diverging_outputs") or []
        confirmed = counterexample.get("confirmed_by_simulation")
        detail = (
            f"diverging outputs: {', '.join(diverging)}"
            if diverging
            else "no diverging outputs reported"
        )
        message = (
            "mapped netlist is NOT equivalent to its pre-mapping generic "
            f"netlist (klt equiv reported 'counterexample'; {detail}; "
            f"confirmed_by_simulation={confirmed})"
        )
    else:
        diagnostics = equiv_report.get("diagnostics") or []
        detail = diagnostics[0]["message"] if diagnostics else "no diagnostic detail"
        message = (
            "equivalence check against the pre-mapping generic netlist did not "
            f"reach a verdict (klt equiv reported '{status}'): {detail}"
        )
    if log_path:
        message += f" -- see {log_path} for the full proof"
    return message


def load_request(request_path: str) -> tuple[dict[str, Any], str]:
    """Read and minimally validate a ``klt.synth.techmap.request/1``
    document (``docs/design/synth-techmap-stage-contract.md`` section 6),
    returning ``(request, request_dir)``.

    Raises :class:`TechmapError` for a missing/unreadable file, invalid
    JSON, or a missing ``generic_netlist``/``liberty`` field -- the same
    two the native binary itself requires.
    """
    try:
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
    except OSError as exc:
        raise TechmapError(f"could not read request '{request_path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TechmapError(
            f"request '{request_path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(request, dict):
        raise TechmapError(f"request '{request_path}' must be a JSON object")
    for field in ("generic_netlist", "liberty"):
        value = request.get(field)
        if not isinstance(value, str) or not value:
            raise TechmapError(
                f"request '{request_path}': '{field}' is required and must be "
                "a non-empty string"
            )
    return request, os.path.dirname(os.path.abspath(request_path))


def _resolve_binary(binary: str | None) -> str:
    """Locate the ``klt-techmap`` binary: an explicit ``binary`` argument,
    then ``$KLT_TECHMAP_BIN``, then this checkout's own
    ``native/techmap/target/release/`` build, then ``$PATH``.

    Raises :class:`TechmapError` naming the ``cargo build`` that produces
    it -- ``native/techmap/`` is a standalone spike crate with no
    ``pyo3``/``maturin`` wheel wiring (see its README), so it is not
    installed alongside this package.
    """
    candidates = [binary, os.environ.get(TECHMAP_BINARY_ENV)]
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    candidates.append(
        os.path.join(
            repo_root, "native", "techmap", "target", "release", TECHMAP_BINARY_NAME
        )
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(TECHMAP_BINARY_NAME)
    if found:
        return found
    raise TechmapError(
        f"{TECHMAP_BINARY_NAME} not found -- build it with "
        "'cargo build --release' in native/techmap/, put it on $PATH, or set "
        f"${TECHMAP_BINARY_ENV} to its path"
    )


def run_techmap(
    request_path: str,
    *,
    verify_equivalence: bool = False,
    equiv_timeout_s: float | None = None,
    binary: str | None = None,
) -> dict[str, Any]:
    """Run the technology-mapping stage declared by the
    ``klt.synth.techmap.request/1`` document at ``request_path``, returning
    its ``klt.synth.techmap.response/1`` response
    (``docs/design/synth-techmap-stage-contract.md`` section 6) with one
    additive field: ``equivalence``.

    ``verify_equivalence`` (default ``False``, additive/opt-in -- unchanged
    behaviour for the standalone binary's own callers) is this stage's
    acceptance gate: before the response is returned, the mapped netlist
    the mapper just produced is proven equivalent to the *pre-mapping
    generic netlist* it was produced from, via
    :func:`verify_mapping_equivalence`. A mapped netlist the gate cannot
    prove equivalent is not accepted -- :class:`TechmapError`, never a
    silent warning or a returned response. ``equiv_timeout_s`` overrides
    ``klt equiv``'s own proof timeout; ``None`` leaves
    :data:`klayout_tools.equiv.DEFAULT_TIMEOUT_S` in effect.
    ``equivalence`` is the gate's summary dict when the gate ran, ``None``
    when it did not.

    Raises :class:`TechmapError` for a bad request, a missing/failed
    ``klt-techmap`` binary, unparseable mapper output, or (when
    ``verify_equivalence`` is set) a failed/inconclusive equivalence check.
    """
    request, request_dir = load_request(request_path)
    executable = _resolve_binary(binary)

    try:
        completed = subprocess.run(
            [executable, os.path.abspath(request_path)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise TechmapError(f"could not launch {executable}: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise TechmapError(
            f"technology mapping failed (exit code {completed.returncode}): "
            f"{detail or 'no output captured'}"
        )

    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TechmapError(
            f"could not parse {TECHMAP_BINARY_NAME}'s response JSON: {exc}"
        ) from exc

    mapped_netlist_path = response.get("mapped_netlist_path")
    if not isinstance(mapped_netlist_path, str) or not mapped_netlist_path:
        raise TechmapError(f"{TECHMAP_BINARY_NAME} returned no 'mapped_netlist_path'")

    equivalence = None
    if verify_equivalence:
        equivalence = verify_mapping_equivalence(
            generic_netlist_path=_resolve_path(request["generic_netlist"], request_dir),
            mapped_netlist_path=mapped_netlist_path,
            liberty_path=_resolve_path(request["liberty"], request_dir),
            timeout_s=equiv_timeout_s,
        )

    response["equivalence"] = equivalence
    return response


def _resolve_path(value: str, base_dir: str) -> str:
    """Resolve a request field's path against the request file's own
    directory -- the same convention the native binary's own ``resolve()``
    uses (``native/techmap/src/main.rs``), so both sides agree on what a
    relative ``generic_netlist``/``liberty`` means."""
    return value if os.path.isabs(value) else os.path.join(base_dir, value)
