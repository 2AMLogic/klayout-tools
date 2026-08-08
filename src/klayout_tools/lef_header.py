"""Read the declarative header attributes of a LEF file that KLayout's own
LEF/DEF importer parses and then discards -- ``SITE`` definitions,
routing-layer ``PITCH``/``OFFSET``/``DIRECTION``, and macro ``PIN``
``DIRECTION``/``USE`` -- without adding an external LEF-parsing dependency.

This is the deferred-trigger resolution
``docs/design/sc-leflib-evaluation.md`` called out: that survey recommended
*against* taking ``sc-leflib`` (a compiled Cython shim around OpenROAD's
si2 LEF parser) and instead reading the header ourselves once a real verb
needed ``SITE``/layer-pitch/pin-direction data -- "~200 lines over ``SITE``,
``LAYER``, ``MACRO`` headers, and ``PIN DIRECTION``/``USE``". This module is
that ~200 lines. :mod:`klayout_tools.lef_abstract` (issue #438, Epic #393
Phase 2) is the first real caller: it needs a tech LEF's ``SITE``/routing-
layer geometry to classify which GDS layers are routing-eligible for the LEF
abstract it emits, and can round-trip-verify its own emitted macro LEF's
``PIN DIRECTION``/``USE`` statements through the same reader.

Scope, deliberately narrow (mirrors KLayout's own importer, which parses
these same header attributes and then discards them -- see
``dbLEFImporter.cc`` citations in ``sc-leflib-evaluation.md``):
:func:`parse_lef_header`/:func:`read_lef_header` read **declarative header
attributes only** -- ``SITE``/``LAYER`` blocks (at the top level of a *tech*
LEF) and a *macro* LEF's ``MACRO``/``PIN`` attributes (``CLASS``, ``SIZE``,
``SITE`` reference, ``SYMMETRY``, pin ``DIRECTION``/``USE``). They never
parse routing/pin **geometry** (``PORT``/``OBS`` ``RECT``/``POLYGON``/
``PATH`` statements, ``VIA``/``VIARULE`` blocks, LEF58 properties) -- that is
exactly the "hard engine" part of LEF (mask-aware, iterated geometry) this
repo already gets for free from KLayout's own importer (``klayout.db``), per
``docs/ARCHITECTURE.md``'s "wrap the proven engine" rule.

One narrow, explicitly-scoped exception (issue #620):
:func:`parse_lef_macro_pin_ports`/:func:`read_lef_macro_pin_ports` read the
*axis-aligned bounding box* of a macro ``PIN``'s ``PORT`` ``RECT``/
``POLYGON`` statements, keyed by the ``PORT``'s own ``LAYER`` name. That is
the **pin access point** ``klt extract --abstract-cells`` needs to know
*where* a black-boxed cell's pin lands so it can be wired into the parent
net graph -- a single point per port, not a routing-grade geometry model.
Everything genuinely "engine-shaped" stays out: ``PATH``/``VIA`` statements
inside a ``PORT`` are skipped, ``ITERATE``/``MASK`` modifiers are ignored,
and ``OBS`` is not read at all. A caller that needs real, mask-aware pin
geometry still reads the same file through ``klayout.db``.

Headless invariant: pure Python text parsing, no ``pya``/``klayout.db``
import at all -- this module has no KLayout dependency.
"""

from __future__ import annotations

import re
from typing import Any

#: Strip everything from a ``#`` to end of line -- LEF's only comment form.
_COMMENT_RE = re.compile(r"#.*")

#: A top-level, block-scoped declaration: ``KEYWORD name`` starting its own
#: line, closed by ``END name`` starting its own line. Verified against a
#: real sky130 tech LEF (``SITE``/``LAYER``) and macro LEF (``MACRO``) --
#: none of these three block kinds nest another same-kind block, so a single
#: non-greedy ``re.findall`` correctly finds every top-level instance without
#: a recursive-descent tokenizer. (``PIN``/``PORT``/``OBS`` blocks nested
#: *inside* a ``MACRO`` are handled separately by :func:`_parse_macro_body`,
#: since ``PORT``/``OBS`` close with a bare ``END`` -- no name -- which this
#: pattern does not match.)
_BLOCK_RE = re.compile(
    r"^[ \t]*(SITE|LAYER|MACRO)[ \t]+(\S+)[ \t]*\n(.*?)^[ \t]*END[ \t]+\2[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

#: A ``PIN name ... END name`` block nested inside a ``MACRO`` body. Its own
#: nested ``PORT ... END`` (bare, no name) is never mistaken for the pin's
#: closing ``END name`` by the same non-greedy-plus-anchored-name logic
#: :data:`_BLOCK_RE` uses.
_PIN_RE = re.compile(
    r"^[ \t]*PIN[ \t]+(\S+)[ \t]*\n(.*?)^[ \t]*END[ \t]+\1[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

_MANUFACTURING_GRID_RE = re.compile(r"\bMANUFACTURINGGRID\s+([0-9.eE+-]+)\s*;")
_DATABASE_MICRONS_RE = re.compile(r"\bDATABASE\s+MICRONS\s+(\d+)\s*;")

_CLASS_RE = re.compile(r"\bCLASS\s+(\S+)\s*;")
_SYMMETRY_RE = re.compile(r"\bSYMMETRY\s+([^;]+);")
_SIZE_RE = re.compile(r"\bSIZE\s+([0-9.eE+-]+)\s+BY\s+([0-9.eE+-]+)\s*;")
_SITE_REF_RE = re.compile(r"^\s*SITE\s+(\S+)\s*;", re.MULTILINE)

_TYPE_RE = re.compile(r"\bTYPE\s+(\S+)\s*;")
_DIRECTION_RE = re.compile(r"\bDIRECTION\s+(\S+)\s*;")
_WIDTH_RE = re.compile(r"\bWIDTH\s+([0-9.eE+-]+)\s*;")
# PITCH/OFFSET take either one value (x == y) or two (x, then y) -- LEF 5.7
# section 5.4 ("Routing Layer Rules").
_PITCH_RE = re.compile(r"\bPITCH\s+([0-9.eE+-]+)(?:\s+([0-9.eE+-]+))?\s*;")
_OFFSET_RE = re.compile(r"\bOFFSET\s+([0-9.eE+-]+)(?:\s+([0-9.eE+-]+))?\s*;")

_USE_RE = re.compile(r"\bUSE\s+(\S+)\s*;")

#: Presence (not geometry) of a nested ``PORT`` block inside a ``PIN`` body --
#: a bare-``END``-closed block (see :data:`_PIN_RE`'s own docstring note on
#: why ``PORT``'s nameless ``END`` cannot be confused with the pin's own
#: named closing ``END <name>``). Detecting *presence* only (not parsing
#: ``RECT``/``POLYGON`` geometry -- this module's own "declarative header
#: attributes only" scope, see the module docstring) is enough to answer
#: "does this pin have any routable connection point at all" -- issue #464's
#: ``klt place-and-route`` macro-pin/netlist-wiring cross-check needs
#: exactly that boolean, not the geometry itself (which it never touches --
#: ``klayout.db`` already owns geometry parsing, per this module's "wrap the
#: proven engine" scope cut).
_PORT_PRESENT_RE = re.compile(r"^[ \t]*PORT\b", re.MULTILINE)

#: A ``PORT ... END`` block nested inside a ``PIN`` body. ``PORT`` closes with
#: a *bare* ``END`` (no name) and never nests another ``PORT``, so a
#: non-greedy match up to the next bare ``END`` line is exact. Only ever
#: applied to a ``PIN`` body already carved out by :data:`_PIN_RE`, so the
#: pin's own ``END <name>`` line is never in range.
_PORT_BLOCK_RE = re.compile(
    r"^[ \t]*PORT[ \t]*$\n(.*?)^[ \t]*END[ \t]*$", re.MULTILINE | re.DOTALL
)

#: One ``KEYWORD <args> ;`` statement inside a ``PORT`` body. LEF geometry
#: statements are all of this shape (``LAYER met1 ;``, ``RECT 0.1 0.2 0.3
#: 0.4 ;``, ``POLYGON x y x y ... ;``), so a single sweep in source order is
#: enough to associate each geometry statement with the ``LAYER`` currently
#: in effect.
_PORT_STATEMENT_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)([^;]*);", re.DOTALL)


def _strip_comments(text: str) -> str:
    return "\n".join(_COMMENT_RE.sub("", line) for line in text.splitlines())


def _float(value: str | None) -> float | None:
    return float(value) if value is not None else None


def _parse_site(name: str, body: str) -> dict[str, Any]:
    class_match = _CLASS_RE.search(body)
    symmetry_match = _SYMMETRY_RE.search(body)
    size_match = _SIZE_RE.search(body)
    return {
        "name": name,
        "class": class_match.group(1) if class_match else None,
        "symmetry": symmetry_match.group(1).split() if symmetry_match else [],
        "width_um": _float(size_match.group(1)) if size_match else None,
        "height_um": _float(size_match.group(2)) if size_match else None,
    }


def _parse_layer(name: str, body: str) -> dict[str, Any]:
    type_match = _TYPE_RE.search(body)
    direction_match = _DIRECTION_RE.search(body)
    width_match = _WIDTH_RE.search(body)
    pitch_match = _PITCH_RE.search(body)
    offset_match = _OFFSET_RE.search(body)

    pitch_x = _float(pitch_match.group(1)) if pitch_match else None
    pitch_y = (
        _float(pitch_match.group(2))
        if pitch_match and pitch_match.group(2)
        else pitch_x
    )
    offset_x = _float(offset_match.group(1)) if offset_match else None
    offset_y = (
        _float(offset_match.group(2))
        if offset_match and offset_match.group(2)
        else offset_x
    )

    return {
        "name": name,
        "type": type_match.group(1) if type_match else None,
        "direction": direction_match.group(1) if direction_match else None,
        "width_um": _float(width_match.group(1)) if width_match else None,
        "pitch_x_um": pitch_x,
        "pitch_y_um": pitch_y,
        "offset_x_um": offset_x,
        "offset_y_um": offset_y,
    }


def _parse_pin(name: str, body: str) -> dict[str, Any]:
    direction_match = _DIRECTION_RE.search(body)
    use_match = _USE_RE.search(body)
    return {
        "name": name,
        "direction": direction_match.group(1) if direction_match else None,
        "use": use_match.group(1) if use_match else None,
        "has_port": bool(_PORT_PRESENT_RE.search(body)),
    }


def _parse_macro(name: str, body: str) -> dict[str, Any]:
    # Macro-level attributes (CLASS/SIZE/SYMMETRY/SITE reference) only ever
    # appear *before* the first PIN/OBS block in a well-formed LEF macro, but
    # searching the whole body is harmless -- DIRECTION/USE/CLASS/SIZE never
    # recur with a different meaning inside a PORT (PORT only ever contains
    # geometry statements: LAYER/RECT/POLYGON/PATH/VIA), so the first match
    # anywhere in the macro body is unambiguously the macro's own attribute.
    class_match = _CLASS_RE.search(body)
    symmetry_match = _SYMMETRY_RE.search(body)
    size_match = _SIZE_RE.search(body)
    site_match = _SITE_REF_RE.search(body)

    pins = [
        _parse_pin(pin_name, pin_body) for pin_name, pin_body in _PIN_RE.findall(body)
    ]
    pins.sort(key=lambda pin: pin["name"])

    return {
        "name": name,
        "class": class_match.group(1) if class_match else None,
        "site": site_match.group(1) if site_match else None,
        "symmetry": symmetry_match.group(1).split() if symmetry_match else [],
        "width_um": _float(size_match.group(1)) if size_match else None,
        "height_um": _float(size_match.group(2)) if size_match else None,
        "pins": pins,
    }


def parse_lef_header(text: str) -> dict[str, Any]:
    """Parse the declarative header attributes out of raw LEF ``text``.

    Returns::

        {
            "database_microns": <int | None>,
            "manufacturing_grid_um": <float | None>,
            "sites": [
                {"name", "class", "symmetry": [...], "width_um", "height_um"}, ...
            ],
            "layers": [
                {"name", "type", "direction", "width_um",
                 "pitch_x_um", "pitch_y_um", "offset_x_um", "offset_y_um"},
                ...
            ],
            "macros": [
                {"name", "class", "site", "symmetry": [...], "width_um",
                 "height_um",
                 "pins": [{"name", "direction", "use", "has_port"}, ...]},
                ...
            ],
        }

    ``sites``/``layers`` come from top-level ``SITE``/``LAYER`` blocks (a
    *tech* LEF's own header); ``macros`` from top-level ``MACRO`` blocks (a
    *cell*/*macro* LEF, or a macro LEF abstract this repo's own
    :mod:`klayout_tools.lef_abstract` emits). A tech LEF with no ``MACRO``
    blocks returns ``"macros": []``; a macro LEF with no top-level
    ``SITE``/``LAYER`` blocks (the common case -- a merged cell LEF or a
    macro abstract does not redeclare the tech stack) returns
    ``"sites": []``/``"layers": []``. Every list is sorted by ``name`` for
    deterministic output.

    A field that a block does not declare is ``None`` (or ``[]`` for
    ``symmetry``/``pins``) -- never guessed, matching this repo's existing
    ``None``-means-absent convention (see ``pdk.py``'s ``_asset_dirs``).

    Never raises on malformed/partial LEF text -- a statement this parser
    does not recognise is simply not extracted; this is a best-effort header
    reader, not a validating LEF grammar (see this module's docstring for
    the deliberate geometry-parsing scope cut).
    """
    clean = _strip_comments(text)

    grid_match = _MANUFACTURING_GRID_RE.search(clean)
    units_match = _DATABASE_MICRONS_RE.search(clean)

    sites: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    for kind, name, body in _BLOCK_RE.findall(clean):
        if kind == "SITE":
            sites.append(_parse_site(name, body))
        elif kind == "LAYER":
            layers.append(_parse_layer(name, body))
        else:  # kind == "MACRO"
            macros.append(_parse_macro(name, body))

    sites.sort(key=lambda site: site["name"])
    layers.sort(key=lambda layer: layer["name"])
    macros.sort(key=lambda macro: macro["name"])

    return {
        "database_microns": int(units_match.group(1)) if units_match else None,
        "manufacturing_grid_um": _float(grid_match.group(1)) if grid_match else None,
        "sites": sites,
        "layers": layers,
        "macros": macros,
    }


def _port_boxes(body: str) -> list[dict[str, Any]]:
    """The ``[{"layer": str, "bbox_um": [x0, y0, x1, y1]}, ...]`` list for one
    ``PORT`` body, one entry per ``RECT``/``POLYGON`` statement, in source
    order.

    A statement seen before any ``LAYER`` statement (malformed LEF) is
    skipped rather than attributed to a guessed layer, and a statement whose
    coordinates do not parse as floats is skipped too -- this is a
    best-effort reader, matching :func:`parse_lef_header`'s own
    never-raise-on-malformed-input contract.
    """
    boxes: list[dict[str, Any]] = []
    layer: str | None = None
    for keyword, raw_args in _PORT_STATEMENT_RE.findall(body):
        key = keyword.upper()
        args = raw_args.split()
        if key == "LAYER":
            layer = args[0] if args else None
            continue
        if layer is None or key not in ("RECT", "POLYGON"):
            # PATH/VIA statements (and any LEF58 extension) are deliberately
            # out of scope -- see the module docstring's scope note.
            continue
        # `MASK <n>` / `ITERATE ...` modifiers may precede the coordinates;
        # keeping only the tokens that parse as numbers drops them without
        # this reader needing a LEF grammar.
        coords: list[float] = []
        for token in args:
            try:
                coords.append(float(token))
            except ValueError:
                continue
        if key == "RECT":
            if len(coords) < 4:
                continue
            x0, y0, x1, y1 = coords[:4]
        else:  # POLYGON -- reduced to its bounding box, see the module docstring
            if len(coords) < 6 or len(coords) % 2:
                continue
            xs = coords[0::2]
            ys = coords[1::2]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        boxes.append(
            {
                "layer": layer,
                "bbox_um": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
            }
        )
    return boxes


def parse_lef_macro_pin_ports(text: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Parse every macro pin's ``PORT`` bounding boxes out of raw LEF ``text``
    (issue #620).

    Returns ``{<macro name>: {<pin name>: [{"layer": <LEF layer name>,
    "bbox_um": [x0, y0, x1, y1]}, ...]}}`` -- one entry per ``RECT``/
    ``POLYGON`` statement inside that pin's ``PORT`` block(s), in source
    order, in **macro-local micrometres** (the frame every LEF ``MACRO``
    declares via ``ORIGIN``; see :mod:`klayout_tools.lef_abstract`, which
    emits exactly this frame).

    A pin with no ``PORT`` geometry at all maps to ``[]`` rather than being
    omitted, so a caller can tell "declared but unroutable" apart from "not
    declared" -- the same distinction ``klt lef-abstract``'s
    ``unroutable_pins[]`` draws.

    Complements :func:`parse_lef_header` rather than extending it: that
    function's returned shape is a published contract several verbs already
    consume, so the geometry lives behind its own entry point. Never raises
    on malformed/partial LEF text, matching :func:`parse_lef_header`.
    """
    clean = _strip_comments(text)
    macros: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for kind, name, body in _BLOCK_RE.findall(clean):
        if kind != "MACRO":
            continue
        pins: dict[str, list[dict[str, Any]]] = {}
        for pin_name, pin_body in _PIN_RE.findall(body):
            boxes: list[dict[str, Any]] = []
            for port_body in _PORT_BLOCK_RE.findall(pin_body):
                boxes.extend(_port_boxes(port_body))
            pins[pin_name] = boxes
        macros[name] = pins
    return macros


def read_lef_macro_pin_ports(path: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """:func:`parse_lef_macro_pin_ports` for the LEF file at ``path``.

    Raises :class:`OSError` (unchanged) if the file cannot be read -- same
    convention as :func:`read_lef_header`.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_lef_macro_pin_ports(handle.read())


def read_lef_header(path: str) -> dict[str, Any]:
    """:func:`parse_lef_header` for the LEF file at ``path``.

    Raises :class:`OSError` (unchanged) if the file cannot be read -- callers
    that need a clean application-level error wrap this themselves
    (mirroring every other ``klt`` module's own file-not-found handling
    convention rather than this shared helper inventing its own exception
    type).
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_lef_header(handle.read())


__all__ = [
    "parse_lef_header",
    "parse_lef_macro_pin_ports",
    "read_lef_header",
    "read_lef_macro_pin_ports",
]
