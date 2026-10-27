"""Issue #2897: back-annotate a flat post-route SDF whose instance names are
escaped identifiers containing a hierarchy-divider character.

A ``generate``-block array of sub-modules, once synthesis/place-and-route
flattens it (Yosys ``flatten``, OpenROAD's post-route ``write_verilog``/
``write_sdf``), turns every leaf cell into an escaped Verilog identifier
whose *name* contains a literal ``.`` (``\\g[0].sub.u1``). The SDF spells
that instance ``g\\[0\\]\\.sub\\.u1``. Icarus 13.0 cannot use that name at
all -- reproduced live against raw ``iverilog -gspecify -ginterconnect`` +
``vvp`` with a hand-written ``$sdf_annotate`` shim:

- in an ``INTERCONNECT`` endpoint it aborts ``vvp`` outright (``ERROR: NULL
  handle passed to vpi_scan.``, ``Assertion `0' failed``, exit 134) --
  issue #1890's crash, which ``klt`` had been rejecting up front;
- in a ``CELL``'s ``(INSTANCE ...)`` it reports ``SDF ERROR: Cannot find
  g[0] in scope ...`` -- Icarus splits the name at the escaped ``.``.

Neither depends on the file's ``(DIVIDER ...)`` header: canonicalizing the
header to ``/`` and rewriting only the *unescaped* separators (the issue's
option A) still aborts ``vvp`` identically, because Icarus splits on an
escaped ``.`` whatever the declared divider is. An escaped ``/`` is only a
problem under ``(DIVIDER /)``; under ``(DIVIDER .)`` it annotates normally
(also verified live), so it is left alone.

What does work (the issue's option D, verified live end to end: annotation
completes with no diagnostic and the annotated delays appear in simulated
time) is to remove the escaped-divider shape entirely: rename each such
instance to a plain Verilog identifier, consistently in a build-dir copy of
the SDF *and* of every source file that declares it. The caller's own
inputs are never modified. Each rename is reported as an additive
``environment.sdf.renamed`` ``{from, to}`` entry so waveform/diagnostic
names can be mapped back to the originals.

Only instance-path components are renamed. An endpoint's final component
(a cell pin, or a bare top-level port) is never renamed -- the testbench
reaches top-level ports by name -- so an escaped-divider pin/port is still
rejected up front by
:func:`~klayout_tools.functional_verification_sdf._reject_sdf_escaped_divider_interconnects`.
"""

from __future__ import annotations

import os
import re

#: One SDF hierarchical path token: escaped characters or anything that is
#: neither whitespace nor a paren.
_SDF_PATH_TOKEN = r"((?:\\.|[^\s()\\])+)"

#: A ``(INSTANCE <path>)`` clause's path (an empty ``(INSTANCE)`` never
#: matches -- it names the design root).
_SDF_INSTANCE_RE = re.compile(r"(\(INSTANCE\s+)" + _SDF_PATH_TOKEN + r"(?=\s*\))")

#: An ``(INTERCONNECT <from> <to> ...`` entry's two endpoints.
_SDF_INTERCONNECT_RE = re.compile(
    r"(\(INTERCONNECT\s+)" + _SDF_PATH_TOKEN + r"(\s+)" + _SDF_PATH_TOKEN
)

#: The ``(DIVIDER <char>)`` header.
_SDF_DIVIDER_RE = re.compile(r"\(DIVIDER\s+(\S+?)\)")

#: A Verilog escaped identifier: a backslash, then everything up to the
#: terminating whitespace.
_VERILOG_ESCAPED_IDENT_RE = re.compile(r"\\(\S+)(?=\s|$)")

#: A Verilog simple identifier (collision checking for generated names).
_VERILOG_SIMPLE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _sdf_divider(text: str) -> str:
    """The SDF's ``DIVIDER`` character (SDF's default, ``.``, when absent)."""
    match = _SDF_DIVIDER_RE.search(text)
    return match.group(1) if match else "."


def _split_sdf_path(token: str, divider: str) -> list[str]:
    """Split an SDF path on every *unescaped* ``divider``, keeping each
    component's own escapes intact."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char == "\\" and index + 1 < len(token):
            current.append(token[index : index + 2])
            index += 2
            continue
        if char == divider:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _sdf_component_is_unresolvable(component: str, divider: str) -> bool:
    """True for a path component Icarus mis-splits: one containing an
    escaped ``.`` (under any divider) or an escaped occurrence of the file's
    own divider."""
    return "\\." in component or ("\\" + divider) in component


def _sdf_unresolvable_pin_tokens(text: str) -> list[str]:
    """Every ``INTERCONNECT`` endpoint whose *final* component (the pin, or a
    bare top-level port) has the unresolvable shape -- the one case
    :func:`_rename_sdf_escaped_divider_instances` does not rewrite."""
    divider = _sdf_divider(text)
    if len(divider) != 1:
        return []
    offenders = []
    for match in _SDF_INTERCONNECT_RE.finditer(text):
        for token in (match.group(2), match.group(4)):
            if _sdf_component_is_unresolvable(
                _split_sdf_path(token, divider)[-1], divider
            ):
                offenders.append(token)
    return offenders


def _sdf_unresolvable_instance_components(text: str, divider: str) -> set[str]:
    """Every instance-path component (an ``INSTANCE`` path's components, an
    ``INTERCONNECT`` endpoint's components bar the final pin) with the
    unresolvable shape, in SDF-escaped form."""
    components: set[str] = set()
    for match in _SDF_INSTANCE_RE.finditer(text):
        components.update(_split_sdf_path(match.group(2), divider))
    for match in _SDF_INTERCONNECT_RE.finditer(text):
        for token in (match.group(2), match.group(4)):
            components.update(_split_sdf_path(token, divider)[:-1])
    return {c for c in components if _sdf_component_is_unresolvable(c, divider)}


def _plain_identifier(verilog_name: str, taken: set[str]) -> str:
    """A simple Verilog identifier for ``verilog_name`` (every character
    outside ``[A-Za-z0-9_]`` becomes ``_``), suffixed ``_klt<n>`` when it
    would collide with an identifier already in use."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", verilog_name)
    if not re.match(r"[A-Za-z_]", base):
        base = "_" + base
    candidate = base
    counter = 1
    while candidate in taken:
        candidate = f"{base}_klt{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


def _rewrite_sdf_paths(text: str, divider: str, mapping: dict[str, str]) -> str:
    """``text`` with every mapped component of every ``INSTANCE`` path and
    ``INTERCONNECT`` endpoint replaced."""

    def _path(token: str) -> str:
        return divider.join(
            mapping.get(part, part) for part in _split_sdf_path(token, divider)
        )

    text = _SDF_INSTANCE_RE.sub(lambda m: m.group(1) + _path(m.group(2)), text)
    return _SDF_INTERCONNECT_RE.sub(
        lambda m: m.group(1) + _path(m.group(2)) + m.group(3) + _path(m.group(4)),
        text,
    )


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _rewrite_sources(
    sources: list[str], verilog_mapping: dict[str, str], output_dir: str
) -> list[str]:
    """``sources`` with every file that declares a renamed escaped identifier
    replaced by a rewritten copy under ``output_dir`` (order preserved; an
    untouched file keeps its original path)."""

    def _replace(match: re.Match[str]) -> str:
        return verilog_mapping.get(match.group(1), match.group(0))

    rewritten = []
    for index, source in enumerate(sources):
        text = _read_text(source)
        new_text = (
            None if text is None else _VERILOG_ESCAPED_IDENT_RE.sub(_replace, text)
        )
        if new_text is None or new_text == text:
            rewritten.append(source)
            continue
        copy_dir = os.path.join(output_dir, "klt_sdf_renamed_sources", str(index))
        os.makedirs(copy_dir, exist_ok=True)
        copy_path = os.path.join(copy_dir, os.path.basename(source))
        with open(copy_path, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        rewritten.append(copy_path)
    return rewritten


def _rename_sdf_escaped_divider_instances(
    sdf_path: str, sources: list[str], output_dir: str
) -> tuple[str, list[str], list[dict[str, str]]] | None:
    """Issue #2897's rename (see the module docstring). Returns ``None`` --
    nothing read beyond the SDF, nothing written -- when the SDF names no
    unresolvable instance; else ``(renamed_sdf_path, sources, renamed)``,
    where ``sources`` has each rewritten file swapped for its build-dir copy
    and ``renamed`` lists ``{"from": <original Verilog name>, "to": <new
    name>}`` sorted by ``from``."""
    text = _read_text(sdf_path)
    if text is None:
        return None
    divider = _sdf_divider(text)
    if len(divider) != 1:
        return None
    components = _sdf_unresolvable_instance_components(text, divider)
    if not components:
        return None

    taken: set[str] = set()
    for source in sources:
        taken.update(_VERILOG_SIMPLE_IDENT_RE.findall(_read_text(source) or ""))
    sdf_mapping: dict[str, str] = {}
    verilog_mapping: dict[str, str] = {}
    for component in sorted(components):
        verilog_name = re.sub(r"\\(.)", r"\1", component)
        if verilog_name not in verilog_mapping:
            verilog_mapping[verilog_name] = _plain_identifier(verilog_name, taken)
        sdf_mapping[component] = verilog_mapping[verilog_name]

    new_sdf_path = os.path.join(output_dir, "klt_sdf_renamed.sdf")
    with open(new_sdf_path, "w", encoding="utf-8") as handle:
        handle.write(_rewrite_sdf_paths(text, divider, sdf_mapping))
    renamed = [
        {"from": name, "to": new} for name, new in sorted(verilog_mapping.items())
    ]
    return new_sdf_path, _rewrite_sources(sources, verilog_mapping, output_dir), renamed


def _apply_sdf_escaped_divider_rename(
    sdf_path: str, sources: list[str], output_dir: str
) -> tuple[str, list[str], list[dict[str, str]]]:
    """:func:`_rename_sdf_escaped_divider_instances` as the first step of
    :func:`~klayout_tools.functional_verification.run_functional_verification`'s
    SDF-normalization pipeline: ``(sdf_path, sources, [])`` unchanged when
    there is nothing to rename (a run on such an SDF is byte-identical to
    before), else the renamed SDF copy, the sources with rewritten copies
    swapped in, and the rename list."""
    result = _rename_sdf_escaped_divider_instances(sdf_path, sources, output_dir)
    if result is None:
        return sdf_path, sources, []
    return result


def _sdf_renamed_report(renamed: list[dict[str, str]]) -> dict[str, list]:
    """``environment.sdf.renamed`` (issue #2897; additive, present only when
    an instance was renamed -- the same absent-otherwise discipline as
    ``environment.sdf.dropped.<class>.arcs``)."""
    return {"renamed": renamed} if renamed else {}
