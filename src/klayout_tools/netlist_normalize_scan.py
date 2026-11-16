"""Read-only SPICE reference-netlist scans for ``klt lvs``.

Split out of ``netlist_normalize.py`` (issue #4746) to keep that module under
its per-file size ceiling. Pure move: every name is re-exported from
``netlist_normalize`` unchanged. This module imports ``netlist_normalize``
only inside the function that needs it, so the import graph stays acyclic.
"""

from __future__ import annotations

import re

from ._paths import fold_spice_continuations


def defined_subckt_names(text: str) -> set[str]:
    """The upper-cased name of every ``.subckt`` defined in SPICE ``text``
    (issue #2952) -- upper-cased because ``NetlistSpiceReader`` reads circuit
    names back that way, so a caller can compare against
    ``kdb.Circuit.name`` directly. ``.include``-d files are not followed."""
    defined: set[str] = set()
    for line in fold_spice_continuations(text.splitlines()):
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == ".subckt":
            defined.add(parts[1].upper())
    return defined


_INCLUDE_LINE_RE = re.compile(r"^(\s*\.(?:include|inc)\s+)(\S.*?)\s*$", re.IGNORECASE)


def quote_spice_includes(text: str, base_dir: str) -> str:
    """Single-quote the path of every unquoted ``.include``/``.inc`` line.

    KLayout's SPICE reader parses an unquoted include path as an expression, so
    a ``-`` in any component truncates it (``/tmp/a-b/c.spice`` -> ``/tmp//a``).
    Relative paths are joined to ``base_dir`` (the reference file's directory)
    so the rewritten text can be read from a temp file elsewhere. Already-
    quoted lines are left untouched; returns ``text`` itself when unchanged.
    """
    import os

    changed = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        match = _INCLUDE_LINE_RE.match(stripped)
        if match and match.group(2)[0] not in "'\"":
            target = os.path.normpath(os.path.join(base_dir, match.group(2)))
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
