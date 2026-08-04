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
``dbLEFImporter.cc`` citations in ``sc-leflib-evaluation.md``): this module
reads **declarative header attributes only** -- ``SITE``/``LAYER`` blocks (at
the top level of a *tech* LEF) and a *macro* LEF's ``MACRO``/``PIN``
attributes (``CLASS``, ``SIZE``, ``SITE`` reference, ``SYMMETRY``, pin
``DIRECTION``/``USE``). It never parses routing/pin **geometry**
(``PORT``/``OBS`` ``RECT``/``POLYGON``/``PATH`` statements, ``VIA``/
``VIARULE`` blocks, LEF58 properties) -- that is exactly the "hard engine"
part of LEF (mask-aware, iterated geometry) this repo already gets for free
from KLayout's own importer (``klayout.db``), per
``docs/ARCHITECTURE.md``'s "wrap the proven engine" rule. A caller that also
needs geometry reads the same file a second time through ``klayout.db``.

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

#: A top-level ``PORT`` statement inside a ``PIN`` body -- ``klt
#: lef-abstract`` (and every real LEF writer this repo has seen) only ever
#: emits ``PORT`` when it has geometry to put in it (see that module's own
#: "Pins" docstring section), so keyword *presence* is a sufficient signal
#: for :data:`_parse_pin`'s ``has_port`` field without also parsing the
#: geometry statements inside it (this module's own deliberate geometry-
#: parsing scope cut, see the module docstring).
_PORT_RE = re.compile(r"^[ \t]*PORT\b", re.MULTILINE)


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
        "has_port": bool(_PORT_RE.search(body)),
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
    ``None``-means-absent convention (see ``pdk.py``'s ``_asset_dirs``). The
    one exception is a pin's ``has_port`` -- always a ``bool``, never
    ``None`` -- ``True`` when the pin's own body declares a top-level
    ``PORT`` statement (geometry present), ``False`` otherwise (e.g. a ``klt
    lef-abstract`` pin whose declared layer did not resolve to a routing LEF
    layer, emitted with no ``PORT`` at all -- see that module's own "Pins"
    docstring section and issue #464).

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


__all__ = ["parse_lef_header", "read_lef_header"]
