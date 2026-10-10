"""The shared ``request.macros`` hard-macro declaration used by ``klt
synthesize``, ``klt sta`` and ``klt place-and-route`` (issue #2635).

A **hard macro** is a pre-characterized block a design instantiates but
carries no RTL for: a compiled SRAM, an analog block emitted by ``klt
lef-abstract``, a third-party IP core shipped as LEF + liberty + GDS.
Before this module existed, each of the three digital verbs described the
design as "RTL/netlist plus one standard-cell library" and a design with one
macro instance failed at a *different* stage in each verb, with none of the
failures recoverable from inside the request document (live-reproduced, and
re-verified for this module against a real ``yosys 0.69``/``OpenROAD
26Q3`` on sky130A):

- ``klt synthesize`` -- ``ERROR: Module `\\sram_8x8' referenced in module
  `\\top' in cell `\\u_sram' is not part of the design.``
- ``klt sta`` -- ``[ERROR ORD-2013] instance u_sram LEF master sram_8x8 not
  found.``
- ``klt place-and-route`` -- the same ``ORD-2013`` at the ``"floorplan"``
  stage, *unless* ``request.macros`` is populated (that verb has carried a
  partial, fixed-placement ``macros`` array since issue #438/#464).

One shape, three verbs
----------------------

The entry shape is deliberately **one concept documented once**, not three
independently-invented schemas (issue #2635's own acceptance criteria).
Every verb accepts these fields with identical names and semantics:

``cell``
    Optional. The macro's cell/master name. Cross-checked against the
    ``MACRO`` name the ``lef`` itself declares, and derived from it when
    omitted -- so a typo is a request error rather than a silent
    "macro you declared is not the macro you supplied".
``lef``
    **Required.** The macro's LEF abstract. Must declare exactly one
    ``MACRO``. This is the field that fixes ``ORD-2013``.
``lib``
    Optional. A **per-corner map** of liberty paths, ``{"<corner>":
    "<path>"}``, keyed by the same corner names ``request.pdk.corner`` /
    ``request.pdk.corners`` use. The reserved key ``"default"`` is an
    explicit catch-all used only when no exact corner key matches. A
    non-empty map with neither an exact nor a ``"default"`` match for the
    corner actually being run is a **named error** (see
    :func:`macro_lib_for_corner`) -- never a silent fallback to the wrong
    corner's timing.
``gds``
    Optional. The macro's GDS view, for stream-out.
``verilog_blackbox``
    Optional. A hand-written ``(* blackbox *)`` module declaration. Only
    ``klt synthesize`` consumes it; when both it and ``lib`` are omitted,
    that verb **generates** one from the LEF's own ``PIN``
    ``DIRECTION``\\ s (:func:`blackbox_verilog_text`), so a caller never has
    to hand-maintain a stub against a vendor port list.

``klt place-and-route`` keeps two additional, placement-specific fields it
has shipped since issue #438 -- ``instance`` (**required** there: the
instance *path* to fix, which is a different thing from ``cell``, the
*master* name), ``x_um``/``y_um``/``orientation`` -- plus ``halo``
(issue #2635). Those live in ``place_and_route.py``'s own
``_validate_macros``, which composes the shared helpers here; nothing in
this module knows about placement.

Why ``lib`` rather than ``liberty``
-----------------------------------

``lib`` is the canonical spelling (it is what issue #2635's own request
sketch uses, and it is short enough to stay readable in a four-corner map).
``liberty`` is accepted as an exact alias everywhere ``lib`` is, because
that is the spelling ``klt place-and-route``'s surrounding request already
uses for other liberty-shaped things and the issue's own acceptance
criterion for that verb names it. Giving **both** in one entry is an error
rather than a precedence puzzle.

Headless invariant: pure Python plus :mod:`klayout_tools.lef_header`'s text
parser -- no ``pya``/``klayout.db`` import, no subprocess.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from klayout_tools.lef_header import read_lef_header

#: The reserved ``macros[].lib`` key meaning "use this liberty for any
#: corner with no exact entry of its own". Spelled as a corner name rather
#: than a sibling field (``lib_default``) so the field stays a plain
#: ``dict[str, str]`` in the JSON contract -- no union type, no second
#: shape for a caller's schema validator to learn.
LIB_DEFAULT_CORNER = "default"

#: The two accepted spellings of the per-corner liberty map, canonical
#: first. See this module's own docstring for why both exist.
_LIB_FIELD_ALIASES = ("lib", "liberty")

#: LEF ``PIN DIRECTION`` -> Verilog port direction, for the blackbox module
#: :func:`blackbox_verilog_text` generates when a macro supplies neither a
#: ``lib`` nor a ``verilog_blackbox``. ``FEEDTHRU`` and a missing/unknown
#: ``DIRECTION`` both degrade to ``inout`` -- the only direction that cannot
#: make Yosys reject a connection it would otherwise have accepted, which is
#: the right failure posture for a port whose real direction the LEF never
#: stated.
_LEF_DIRECTION_TO_VERILOG = {
    "INPUT": "input",
    "OUTPUT": "output",
    "INOUT": "inout",
    "FEEDTHRU": "inout",
}

#: A legal Verilog 2001 simple identifier -- what a macro cell/pin name must
#: be for :func:`blackbox_verilog_text` to emit it unescaped. A name outside
#: this grammar is emitted in Verilog's escaped form (``\\name `` -- the
#: trailing space is part of the token), never silently rewritten.
_SIMPLE_VERILOG_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")

#: A LEF bus-bit pin name (``A[3]``) under LEF's default ``BUSBITCHARS``
#: (``[]``): the bus base and the bit index.
_LEF_BUS_BIT_RE = re.compile(r"(?P<base>.+)\[(?P<index>\d+)\]\Z")

#: The ``ORD-2013`` diagnostic every OpenROAD-backed verb emits when the
#: netlist instantiates a master no ``read_lef`` ever loaded -- i.e. exactly
#: the "macro referenced in the netlist but absent from ``request.macros``"
#: case. Captured so :func:`missing_lef_master_hint` can turn it into a
#: request-level instruction instead of leaving a caller to search the
#: OpenROAD source for what a LEF master is.
_ORD_2013_RE = re.compile(
    r"\[ERROR ORD-2013\]\s*instance\s+(?P<instance>\S+)\s+LEF master\s+"
    r"(?P<master>\S+)\s+not found"
)

#: Yosys's counterpart diagnostic: a module instantiated but never read.
#: Yosys prefixes a public identifier with a backslash in diagnostics
#: (``\\sram_8x8``), which this strips so the hint names the cell the way a
#: request would spell it.
_YOSYS_UNDECLARED_MODULE_RE = re.compile(
    r"ERROR:\s*Module\s+`\\?(?P<module>[^']+)'\s+referenced in module\s+"
    r"`\\?(?P<parent>[^']+)'\s+in cell\s+`\\?(?P<instance>[^']+)'\s+"
    r"is not part of the design"
)


def resolve_macro_path(value: str, request_dir: str) -> str:
    """``value`` as an absolute path, resolved against ``request_dir`` when
    relative -- the same "relative to the request file's own directory"
    convention every other path field in every ``klt`` request uses."""
    return os.path.abspath(
        value if os.path.isabs(value) else os.path.join(request_dir, value)
    )


def _require_non_empty_string(
    value: Any, *, label: str, error_cls: type[Exception]
) -> str:
    if not isinstance(value, str) or not value:
        raise error_cls(f"{label} is required and must be a non-empty string")
    return value


def resolve_macro_lef(
    entry: dict[str, Any],
    *,
    label: str,
    request_dir: str,
    error_cls: type[Exception],
) -> tuple[str, dict[str, Any]]:
    """``(absolute lef path, the single ``MACRO`` record it declares)``.

    ``label`` is the request-field path to name in any error (e.g.
    ``"request.macros[0]"``), so one helper serves three verbs without
    inventing three message dialects.

    Requires **exactly one** ``MACRO`` per file, the same invariant
    ``place_and_route.py``'s own macro validation has enforced since issue
    #438: a per-entry ``lef`` naming a multi-macro library would leave
    ``cell`` ambiguous, and the cell-name cross-check below is the thing
    that makes a typo'd ``cell`` a request error rather than a mis-wired
    run.
    """
    lef = _require_non_empty_string(
        entry.get("lef"), label=f"{label}.lef", error_cls=error_cls
    )
    lef_path = resolve_macro_path(lef, request_dir)
    if not os.path.isfile(lef_path):
        raise error_cls(f"{label}.lef not found: {lef}")
    macro_cells = read_lef_header(lef_path)["macros"]
    if len(macro_cells) != 1:
        raise error_cls(
            f"{label}.lef '{lef}' must declare exactly one MACRO "
            f"(found {len(macro_cells)})"
        )
    return lef_path, macro_cells[0]


def resolve_macro_cell_name(
    entry: dict[str, Any],
    *,
    label: str,
    lef_cell_name: str,
    error_cls: type[Exception],
) -> str:
    """The macro's cell/master name: ``entry["cell"]`` when given (and then
    required to match the ``MACRO`` name the LEF itself declares), else the
    LEF's own name.

    The cross-check is the point: a ``cell`` that disagrees with its own
    ``lef`` is always a mistake, and catching it here costs nothing compared
    with discovering it as an ``ORD-2013`` several stages into a real run.
    """
    cell = entry.get("cell")
    if cell is None:
        return lef_cell_name
    if not isinstance(cell, str) or not cell:
        raise error_cls(f"{label}.cell must be a non-empty string when given")
    if cell != lef_cell_name:
        raise error_cls(
            f"{label}.cell '{cell}' does not match the MACRO name its own lef "
            f"declares ('{lef_cell_name}')"
        )
    return cell


def resolve_optional_macro_file(
    entry: dict[str, Any],
    key: str,
    *,
    label: str,
    request_dir: str,
    error_cls: type[Exception],
) -> str | None:
    """One optional path field (``gds``, ``verilog_blackbox``) as an
    absolute path, or ``None`` when omitted. A field that is present but
    unusable (wrong type, empty, missing file) is always an error -- never
    silently treated as omitted."""
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise error_cls(f"{label}.{key} must be a non-empty string when given")
    path = resolve_macro_path(value, request_dir)
    if not os.path.isfile(path):
        raise error_cls(f"{label}.{key} not found: {value}")
    return path


def resolve_macro_lib(
    entry: dict[str, Any],
    *,
    label: str,
    request_dir: str,
    error_cls: type[Exception],
) -> dict[str, str] | None:
    """The macro's per-corner liberty map as ``{corner: absolute path}``, or
    ``None`` when neither ``lib`` nor ``liberty`` is given.

    Accepts either spelling (see this module's docstring) but never both in
    the same entry -- two names for one field is a documentation choice, two
    *values* for one field is a precedence puzzle with no right answer.
    """
    present = [name for name in _LIB_FIELD_ALIASES if entry.get(name) is not None]
    if len(present) > 1:
        raise error_cls(
            f"{label} must give at most one of "
            + "/".join(f"'{name}'" for name in _LIB_FIELD_ALIASES)
            + " -- they are two spellings of the same per-corner liberty map"
        )
    if not present:
        return None
    field = present[0]
    value = entry[field]
    if not isinstance(value, dict):
        raise error_cls(
            f"{label}.{field} must be an object mapping a corner name to a "
            'liberty path (e.g. {"tt_025C_1v80": "sram_tt.lib"}); the '
            f"reserved key '{LIB_DEFAULT_CORNER}' applies to any corner with "
            "no entry of its own"
        )
    if not value:
        raise error_cls(f"{label}.{field} must not be empty when given")
    resolved: dict[str, str] = {}
    for corner, lib in value.items():
        if not isinstance(corner, str) or not corner:
            raise error_cls(f"{label}.{field} keys must be non-empty corner names")
        if not isinstance(lib, str) or not lib:
            raise error_cls(f"{label}.{field}['{corner}'] must be a non-empty string")
        lib_path = resolve_macro_path(lib, request_dir)
        if not os.path.isfile(lib_path):
            raise error_cls(f"{label}.{field}['{corner}'] not found: {lib}")
        resolved[corner] = lib_path
    return dict(sorted(resolved.items()))


def macro_lib_for_corner(
    macro: dict[str, Any],
    corner: str | None,
    *,
    error_cls: type[Exception],
    field: str = "request.macros",
) -> str | None:
    """The liberty path to load for ``macro`` at ``corner``, or ``None``
    when the macro declared no ``lib`` at all (a LEF-only macro -- legal:
    the LEF alone is what fixes ``ORD-2013``; the engine then treats the
    instance as an untimed blackbox).

    Resolution order is exact corner key, then :data:`LIB_DEFAULT_CORNER`.
    A macro that *did* declare a ``lib`` map with neither is a hard error
    naming the macro, the corner being run, and the keys that *are*
    available -- this is issue #2635's "a macro liberty with no matching
    corner in a multi-corner STA run" edge case, and a silent fallback to
    some other corner's timing would be a wrong answer reported as a right
    one.
    """
    lib = macro.get("lib")
    if not lib:
        return None
    if corner is not None and corner in lib:
        return lib[corner]
    if LIB_DEFAULT_CORNER in lib:
        return lib[LIB_DEFAULT_CORNER]
    raise error_cls(
        f"{field} entry for macro '{macro['cell']}' declares a liberty for "
        + ", ".join(sorted(lib))
        + f" but nothing for corner '{corner}' (and no "
        f"'{LIB_DEFAULT_CORNER}' entry) -- add that corner's macro liberty, "
        f"or key one entry '{LIB_DEFAULT_CORNER}' to use it at every corner"
    )


def validate_macros(
    macros: Any,
    request_dir: str,
    *,
    error_cls: type[Exception],
    field: str = "request.macros",
) -> list[dict[str, Any]]:
    """Validate the shared, placement-free ``request.macros`` array that
    ``klt synthesize`` and ``klt sta`` accept, returning one normalized
    entry per macro::

        {"cell": str, "lef": abs, "lib": {corner: abs} | None,
         "gds": abs | None, "verilog_blackbox": abs | None,
         "width_um": float | None, "height_um": float | None}

    Returns ``[]`` when the field is omitted or ``None`` -- purely additive,
    so every request written before this field existed behaves exactly as
    it did.

    ``cell`` values must be unique: two entries for one master would make
    "which liberty is this cell's" ambiguous, and the duplicate is always a
    copy-paste slip rather than a design intent. (``klt place-and-route``
    additionally allows several *instances* of the same master; its own
    validator enforces uniqueness over ``instance`` instead -- see
    ``place_and_route.py``.)
    """
    if macros is None:
        return []
    if not isinstance(macros, list):
        raise error_cls(f"{field} must be a list")

    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(macros):
        label = f"{field}[{index}]"
        if not isinstance(entry, dict):
            raise error_cls(f"{label} must be an object")
        lef_path, macro_record = resolve_macro_lef(
            entry, label=label, request_dir=request_dir, error_cls=error_cls
        )
        cell = resolve_macro_cell_name(
            entry,
            label=label,
            lef_cell_name=macro_record["name"],
            error_cls=error_cls,
        )
        validated.append(
            {
                "cell": cell,
                "lef": lef_path,
                "lib": resolve_macro_lib(
                    entry,
                    label=label,
                    request_dir=request_dir,
                    error_cls=error_cls,
                ),
                "gds": resolve_optional_macro_file(
                    entry,
                    "gds",
                    label=label,
                    request_dir=request_dir,
                    error_cls=error_cls,
                ),
                "verilog_blackbox": resolve_optional_macro_file(
                    entry,
                    "verilog_blackbox",
                    label=label,
                    request_dir=request_dir,
                    error_cls=error_cls,
                ),
                "width_um": macro_record.get("width_um"),
                "height_um": macro_record.get("height_um"),
                "pins": macro_record.get("pins") or [],
            }
        )

    cells = [macro["cell"] for macro in validated]
    if len(cells) != len(set(cells)):
        raise error_cls(f"{field}[].cell values must be unique")
    return validated


def macros_response(
    macros: list[dict[str, Any]],
    *,
    normalize_path: Callable[[str | None], Any] | None = None,
) -> list[dict[str, Any]]:
    """The echo of ``request.macros`` a verb's own JSON response carries --
    exactly the shared fields, with the internal LEF-geometry fields
    (``width_um``/``height_um``/``pins``) dropped.

    One function so no two verbs can drift into slightly different echoes of
    the same declaration.

    ``normalize_path`` is how each verb applies **its own** established
    path-reporting convention to these fields without this module having to
    know about any of them: ``klt sta``/``klt place-and-route`` report raw
    absolute paths (as their existing ``def_path``/``macros[].lef`` fields
    already do, so they pass ``None`` here), while ``klt synthesize``
    reports every path as the ``{path, scope}`` shape
    :func:`klayout_tools.env_provenance.repo_relative_path` defines -- the
    convention that verb bumped its own ``schema_version`` to 2 for, and
    which its report-envelope lint enforces. The *field names and
    semantics* are identical either way; only the per-verb path encoding
    differs, which is a pre-existing divergence this field inherits rather
    than one it introduces.
    """
    encode = normalize_path if normalize_path is not None else (lambda path: path)
    return [
        {
            "cell": macro["cell"],
            "lef": encode(macro["lef"]),
            "lib": (
                None
                if macro["lib"] is None
                else {corner: encode(path) for corner, path in macro["lib"].items()}
            ),
            "gds": encode(macro["gds"]),
            "verilog_blackbox": encode(macro["verilog_blackbox"]),
        }
        for macro in macros
    ]


def _verilog_identifier(name: str) -> str:
    """``name`` as a Verilog identifier token -- bare when it already is a
    legal simple identifier, else Verilog's escaped form (``\\name``
    followed by the space that terminates the escape). Never rewritten: a
    macro port named ``A[0]`` keeps that exact name, which is what makes the
    generated stub actually link against the vendor's own netlist."""
    if _SIMPLE_VERILOG_IDENTIFIER_RE.match(name):
        return name
    return f"\\{name} "


def _blackbox_ports(
    macro: dict[str, Any], *, error_cls: type[Exception]
) -> list[tuple[str, str, str]]:
    """``(name, verilog_direction, range_text)`` per port of ``macro``'s
    blackbox, in first-appearance order, with LEF bus bits (``A[0]``..)
    folded into one vector port named by the bus base."""
    cell = macro["cell"]
    ports: list[tuple[str, str, str]] = []
    buses: dict[str, dict[int, str]] = {}
    scalar_names: set[str] = set()
    slots: dict[str, int] = {}
    for pin in macro.get("pins") or []:
        name = pin["name"]
        direction = _LEF_DIRECTION_TO_VERILOG.get(pin.get("direction") or "", "inout")
        match = _LEF_BUS_BIT_RE.match(name)
        if match is None:
            scalar_names.add(name)
            ports.append((name, direction, ""))
            continue
        base = match.group("base")
        bits = buses.setdefault(base, {})
        if base not in slots:
            slots[base] = len(ports)
            ports.append((base, direction, ""))
        index = int(match.group("index"))
        if index in bits:
            raise error_cls(
                f"macro '{cell}': LEF pin '{name}' is declared more than once"
            )
        bits[index] = direction
    for base, bits in buses.items():
        if base in scalar_names:
            raise error_cls(
                f"macro '{cell}': LEF pin '{base}' is both a scalar pin and the "
                f"base of bus pins '{base}[n]'; cannot generate a blackbox port"
            )
        low, high = min(bits), max(bits)
        if len(bits) != high - low + 1:
            raise error_cls(
                f"macro '{cell}': LEF bus '{base}' has non-contiguous bits "
                f"{sorted(bits)}; supply the macro's verilog_blackbox or lib "
                "instead of a LEF-derived stub"
            )
        directions = set(bits.values())
        if len(directions) != 1:
            raise error_cls(
                f"macro '{cell}': LEF bus '{base}' mixes pin directions "
                f"{sorted(directions)}; supply the macro's verilog_blackbox or "
                "lib instead of a LEF-derived stub"
            )
        ports[slots[base]] = (base, directions.pop(), f" [{high}:{low}]")
    return ports


def blackbox_verilog_text(
    macro: dict[str, Any], *, error_cls: type[Exception] = ValueError
) -> str:
    """A ``(* blackbox *)`` Verilog module declaration for ``macro``,
    derived from its LEF's own ``PIN`` ``DIRECTION``\\ s.

    This is the "or synthesizes a blackbox from the LEF" half of issue
    #2635's synthesize acceptance criterion, and the reason a caller never
    has to hand-maintain a stub: the port list comes from the same file the
    physical flow reads, so it cannot drift from the vendor's port list by
    eye the way a hand-written stub does.

    ``read_verilog -lib`` already marks every module in the file as a
    blackbox; the explicit ``(* blackbox *)`` attribute is belt-and-braces
    so the artifact is still correct if a reader (or a future caller) loads
    it without that flag. Verified live against Yosys 0.69: a ``top`` whose
    only macro instance is declared this way elaborates, survives
    ``synth``/``abc``, and appears in ``stat``'s own
    ``num_cells_by_type`` -- the instance is preserved rather than
    implemented out of standard cells.

    LEF bus pins (``PIN A[0]``, ``PIN A[1]``, ...) are grouped into one
    Verilog vector port (``input [1:0] A;``): a source instantiation such as
    ``.A(addr)`` needs the vector ``A``, and escaped scalars named ``A[0]``
    are distinct identifiers that no named connection to ``A`` can reach. A
    bus whose shape the stub cannot represent faithfully (gapped indices,
    mixed directions, or a scalar pin sharing the bus's base name) raises
    ``error_cls`` rather than emitting an incompatible stub.
    """
    cell = macro["cell"]
    ports = _blackbox_ports(macro, error_cls=error_cls)
    port_names = [name for name, _, _ in ports]
    declarations = [
        f"  {direction}{rng} {_verilog_identifier(name)};"
        for name, direction, rng in ports
    ]
    header = (
        f"// Generated by klt from {os.path.basename(macro['lef'])} -- do not edit.\n"
        f"// Blackbox declaration for hard macro '{cell}' (request.macros).\n"
        "(* blackbox *)\n"
    )
    port_list = ", ".join(_verilog_identifier(name) for name in port_names)
    body = "\n".join(declarations)
    return (
        f"{header}module {_verilog_identifier(cell)} ({port_list});\n"
        + (f"{body}\n" if body else "")
        + "endmodule\n"
    )


def missing_lef_master_hint(text: str, *, field: str = "request.macros") -> str | None:
    """``None``, or an actionable hint that an OpenROAD run failed because
    the netlist instantiates a master no ``read_lef`` ever loaded -- the
    ``ORD-2013`` shape.

    Issue #2635's test plan calls this out explicitly: "a macro referenced
    in the netlist but absent from ``request.macros`` must still produce a
    clear, named error rather than ``ORD-2013``". ``ORD-2013`` names the
    instance and the master but says nothing about *which request field*
    would have supplied it, and the master name is exactly the ``cell``
    value the caller needs -- so the hint restates it as the request edit to
    make.
    """
    match = _ORD_2013_RE.search(text)
    if match is None:
        return None
    master = match.group("master")
    instance = match.group("instance")
    return (
        f"instance '{instance}' is a hard macro ('{master}') with no "
        f"physical view loaded: declare it in {field} as "
        f'{{"cell": "{master}", "lef": "<{master}.lef>"}} (add a per-corner '
        '"lib" to time it too)'
    )


def undeclared_module_hint(text: str, *, field: str = "request.macros") -> str | None:
    """``None``, or the Yosys-side counterpart of
    :func:`missing_lef_master_hint`: a module instantiated by the sources
    but never read into the design.

    Deliberately *not* unconditional advice to add a macro -- the same Yosys
    error is what a genuinely missing RTL file produces -- so the hint names
    both dispositions and leaves the choice to the caller. What it must
    never do is leave a caller who *does* have a hard macro guessing that
    the fix is to feed the vendor's behavioural Verilog into ``sources``,
    which makes the synthesizer implement the macro out of standard cells
    (issue #2635: "worse than nothing").
    """
    match = _YOSYS_UNDECLARED_MODULE_RE.search(text)
    if match is None:
        return None
    module = match.group("module")
    instance = match.group("instance")
    return (
        f"module '{module}' (instance '{instance}') is instantiated but never "
        f"read: add its source to request.sources, or -- if it is a hard "
        f"macro -- declare it in {field} as "
        f'{{"cell": "{module}", "lef": "<{module}.lef>"}} so it is '
        "blackboxed instead of synthesized from standard cells"
    )
