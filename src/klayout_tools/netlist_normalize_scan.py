"""Read-only SPICE reference-netlist scans for ``klt lvs``.

Split out of ``netlist_normalize.py`` (issue #4746) to keep that module under
its per-file size ceiling. This module imports ``netlist_normalize`` only
inside the function that needs it, so the import graph stays acyclic.
"""

from __future__ import annotations

import gzip
import os
import re
import zlib

from ._spice_text import fold_spice_continuations

_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)", re.IGNORECASE)
_INCLUDE_LINE_RE = re.compile(r"^(\s*\.(?:include|inc)\s+)(\S.*?)\s*$", re.IGNORECASE)
_LIB_LINE_RE = re.compile(r"^\s*\.lib\s+(.+?)\s*$", re.IGNORECASE)
# A `.lib` argument is a bare token or a single-/double-quoted string.
_LIB_ARGUMENT_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')
_ENDL_RE = re.compile(r"^\s*\.endl(?:\s|$)", re.IGNORECASE)


def defined_subckt_names(text: str) -> set[str]:
    """The upper-cased name of every ``.subckt`` defined in SPICE ``text``
    (issue #2952) -- upper-cased because ``NetlistSpiceReader`` reads circuit
    names back that way, so a caller can compare against
    ``kdb.Circuit.name`` directly. ``.include``-d files are not followed."""
    return {
        match.group(1).upper()
        for line in fold_spice_continuations(text.splitlines())
        if (match := _SUBCKT_RE.match(line))
    }


def _include_path(base_dir: str, target: str) -> str:
    return os.path.normpath(os.path.join(base_dir, target))


def _unquote(target: str) -> str:
    if len(target) >= 2 and target[0] == target[-1] and target[0] in "\"'":
        return target[1:-1]
    return target


def _lib_arguments(line: str) -> list[str] | None:
    """The arguments of a ``.lib`` line, or ``None`` for any other line.

    One argument is the section-opening form (``.lib tt``); two are the
    corner-selected library reference (``.lib 'device corners.lib' tt``),
    with quotes stripped so a path may contain spaces. Anything else is not
    a form the scan follows.
    """
    match = _LIB_LINE_RE.match(line)
    if match is None:
        return None
    return [
        double or single or bare
        for double, single, bare in _LIB_ARGUMENT_RE.findall(match.group(1))
    ]


def _scan_recovery_file(
    text: str, source_path: str, section: str | None = None
) -> tuple[set[str], list[tuple[str, str | None]]]:
    """Deck class names defined in ``text`` and the directives to follow.

    With ``section`` set -- this file was reached through a two-argument
    ``.lib <file> <section>`` line -- only the named section's ``.SUBCKT``
    names are collected (matched case-insensitively, like KLayout) and only
    directives inside that section are followed, because KLayout reads
    nothing else from a selected file. Without one -- the top-level file or
    a plain include -- every ``.lib <name> ... .endl`` region is skipped:
    an unselected section's definitions are not read that way either.

    Each followed directive is returned as ``(path, section)`` with the
    section the *target* is to be read in, ``None`` for a plain include.
    """
    base_dir = os.path.dirname(source_path)
    names: set[str] = set()
    children: list[tuple[str, str | None]] = []
    in_library_section = False
    inside_selected = False
    wanted = section.upper() if section is not None else None
    for line in fold_spice_continuations(text.splitlines()):
        arguments = _lib_arguments(line)
        if arguments is not None and len(arguments) == 1:
            # One argument opens a named section. In a section-selected
            # scan only the wanted section's region is active; in a plain
            # scan every section's region is skipped.
            in_library_section = True
            inside_selected = arguments[0].upper() == wanted
        elif _ENDL_RE.match(line):
            in_library_section = False
            inside_selected = False
        elif not (inside_selected or (wanted is None and not in_library_section)):
            # A selected scan reads nothing outside its section; a plain
            # scan reads nothing inside any (unselected) section. Both
            # match what KLayout's own reader does with the file.
            continue
        elif match := _SUBCKT_RE.match(line):
            names.add(match.group(1).upper())
        elif match := _INCLUDE_LINE_RE.match(line):
            target = match.group(2)
            if target[0] == target[-1] and target[0] in "\"'":
                target = target[1:-1]
            children.append((_include_path(base_dir, target), None))
        elif arguments is not None and len(arguments) == 2:
            # The two-argument form selects a section of another file.
            children.append(
                (_include_path(base_dir, _unquote(arguments[0])), arguments[1])
            )
    return names, children


def _read_include(path: str) -> str:
    # Like KLayout, recognize a gzip stream by its magic bytes.
    with open(path, "rb") as stream:
        if stream.peek(2).startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=stream) as compressed:
                return compressed.read().decode("utf-8", errors="replace")
        return stream.read().decode("utf-8", errors="replace")


def recovery_subckt_names(
    text: str, source_path: str, candidates: set[str]
) -> set[str]:
    """Deck class names defined as circuits in this netlist or its includes.

    Start with the text and path passed to KLayout (possibly a converted
    reference). Follow ``.include``/``.inc`` and corner-selected
    ``.lib <file> <section>`` directives alike. Leave missing or unreadable
    include diagnostics to its reader.
    """
    if not candidates:
        return set()
    # KLayout anchors nested paths at the real file, including through symlinks.
    source_path = os.path.realpath(source_path)
    names, pending = _scan_recovery_file(text, source_path)
    defined = names & candidates
    seen = {(source_path, None)}
    while pending and defined != candidates:
        path, section = pending.pop()
        if not os.path.isfile(path):
            continue
        # One file selected for two different sections is read twice -- the
        # sections define different circuits -- so the visited key carries
        # the selection.
        real_path = os.path.realpath(path)
        if (real_path, section) in seen:
            continue
        seen.add((real_path, section))
        try:
            included = _read_include(real_path)
        except (OSError, EOFError, zlib.error):
            continue
        names, children = _scan_recovery_file(included, real_path, section)
        defined.update(names & candidates)
        pending.extend(children)
    return defined


def quote_spice_includes(text: str, base_dir: str) -> str:
    """Single-quote the path of every unquoted ``.include``/``.inc`` line.

    KLayout's SPICE reader parses an unquoted include path as an expression, so
    a ``-`` in any component truncates it (``/tmp/a-b/c.spice`` -> ``/tmp//a``).
    Relative paths are joined to ``base_dir`` (the reference file's directory)
    so the rewritten text can be read from a temp file elsewhere. Already-
    quoted lines are left untouched; returns ``text`` itself when unchanged.
    """
    changed = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        match = _INCLUDE_LINE_RE.match(stripped)
        if match and match.group(2)[0] not in "'\"":
            target = _include_path(base_dir, match.group(2))
            if "'" not in target:
                line = f"{match.group(1)}'{target}'{ending}"
                changed = True
        out.append(line)
    return "".join(out) if changed else text


def detect_subckt_call_devices(text: str) -> list[str]:
    """Return the curated PDK device subcircuit names an ``X`` card in ``text``
    instantiates *without* a matching ``.subckt`` definition in the same file.

    Used by ``klt lvs`` to turn the silent subckt-call degradation into a
    specific, actionable error when the caller did *not* opt into conversion:
    a non-empty result means the reference netlist is in the simulation
    (subckt-call) form, not the plain-element form ``klt lvs`` requires. A
    device subcircuit that *is* defined in the file is not reported -- the
    reader can at least read it as a subcircuit, a different (out-of-scope)
    case.

    Covers every curated device family (issue #1130: MOS, resistor,
    capacitor, bipolar), not just MOS -- a reference netlist whose only
    subckt-call devices are e.g. resistors is now detected the same way a
    MOS-only one always was.

    A card whose instance name contains ``$`` (``X$1``, the name KLayout's
    own extractor writes) is reported only when it is a MOS call: bare
    ``klt extract`` round-trips resistors/capacitors/bipolars as such cards
    under the same curated names (issues #1157/#1942), so those are not
    evidence of the simulation form. Only ``klt extract --pdk`` emits MOS
    ``X`` cards (issue #2972).
    """
    from .netlist_normalize import _split_params, _tokenize, known_device_subckt_names

    known = known_device_subckt_names()
    defined: set[str] = set()
    used: list[str] = []
    seen: set[str] = set()

    for line in fold_spice_continuations(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.lower()
        if lowered.startswith(".subckt"):
            parts = stripped.split()
            if len(parts) >= 2:
                defined.add(parts[1])
            continue
        first = stripped.split(None, 1)[0]
        if not first or first[0] not in "Xx":
            continue
        tokens = _tokenize(line)
        positional, _params = _split_params(tokens[1:])
        if not positional:
            continue
        subckt_name = positional[-1]
        if subckt_name not in known or subckt_name in seen:
            continue
        if "$" in tokens[0] and known[subckt_name][1].kind != "mos":
            continue
        used.append(subckt_name)
        seen.add(subckt_name)

    return [name for name in used if name not in defined]
