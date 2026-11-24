"""Library scan behind ``monte_carlo.mismatch_scale`` (issues #5486, #7671).

Reads the mismatch-sigma ``.param <name>_mm = <number>`` cards out of the
``.lib <section>`` block a ``*_mismatch`` process section selects, following
its ``.include`` / nested ``.lib <file> <section>`` closure and skipping
``.subckt`` bodies. Split out of :mod:`sim_mismatch_scale` so that module stays
under the per-file size ratchet; it resolves nothing itself -- the policy (which
sections, what to emit) stays there.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from typing import Any

from ._spice_number import parse as _spice_literal
from .sim_errors import SimError

#: Suffix that marks a mismatch-sigma ``.param`` in a scanned library.
SIGMA_SUFFIX = "_mm"

_LIB_START_RE = re.compile(r"^\s*\.lib\s+(\S+)(?:\s+(\S+))?\s*$", re.IGNORECASE)
_LIB_END_RE = re.compile(r"^\s*\.endl\b", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.inc(?:lude)?\s+(\S+)\s*$", re.IGNORECASE)
_PARAM_RE = re.compile(r"^\s*\.param\s+(.*)$", re.IGNORECASE)
_SUBCKT_RE = re.compile(r"^\s*\.subckt\b", re.IGNORECASE)
_SUBCKT_END_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
#: One ``<name> = <value>`` assignment of a ``.param`` card; a quoted or
#: braced right-hand side is consumed as one unit, so an ``agauss(...)``
#: expression that *mentions* a sigma is never rescanned as an assignment.
_ASSIGN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*('[^']*'|\"[^\"]*\"|\{[^}]*\}|[^\s=]+)"
)
_COMMENT_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\{[^}]*\}|([$;])")


def _read(field: str, path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        raise SimError(f"{field} cannot read model library {path}: {exc}") from exc


def _logical_lines(raw: list[str]) -> list[str]:
    """``raw`` with SPICE ``+`` continuations folded onto their own line."""
    out: list[str] = []
    for line in raw:
        if line.lstrip().startswith("+") and out:
            out[-1] += " " + line.lstrip()[1:]
        else:
            out.append(line)
    return out


def _section_sigma_params(field: str, path: str, section: str) -> dict[str, float]:
    """Every ``.param <name>_mm = <number>`` the ``.lib <section>`` block of
    ``path`` defines, following its ``.include`` / nested ``.lib`` closure."""
    found: dict[str, float] = {}
    _walk_section(field, path, section, found, set())
    return found


def _top_level(lines: Iterable[str]) -> Iterator[str]:
    """Only the lines outside every ``.subckt`` body: a parameter declared
    there is subcircuit-local, not the deck-level sigma."""
    depth = 0
    for line in lines:
        if _SUBCKT_RE.match(line):
            depth += 1
        elif _SUBCKT_END_RE.match(line):
            depth = max(0, depth - 1)
        elif not depth:
            yield line


def _walk_section(
    field: str, path: str, section: str, found: dict[str, float], seen: set[Any]
) -> None:
    key = (os.path.abspath(path), section.lower())
    if key in seen:
        return
    seen.add(key)
    lines = _top_level(_logical_lines(_read(field, path)))
    inside = False
    for line in lines:
        start = _LIB_START_RE.match(line)
        if start is not None and start.group(2) is None:
            if inside:
                # SPICE sections do not nest, so a one-argument `.lib` inside
                # an open section can only be an include form; reading it as
                # an opener would end the scan here and scale only the sigmas
                # before it (issue #7671).
                raise SimError(
                    f"{field} cannot scale mismatch-sigma parameters across "
                    f"{line.strip()!r} in {path}: a one-argument .lib inside "
                    f"the selected {section!r} section is ambiguous (a "
                    "section name or an include) -- refusing rather than "
                    "scaling only the parameters before it"
                )
            # `.lib <section>` opens a named block of this same file.
            inside = start.group(1).lower() == section.lower()
            continue
        if _LIB_END_RE.match(line):
            inside = False
            continue
        if inside:
            _walk_line(field, path, line, found, seen)


def _walk_line(
    field: str, path: str, line: str, found: dict[str, float], seen: set[Any]
) -> None:
    """One line inside a selected section: recurse into what it pulls in, or
    record the sigma ``.param``s it defines."""
    include = _INCLUDE_RE.match(line)
    if include is not None:
        _walk_file(field, _join(path, include.group(1)), found, seen)
        return
    nested = _LIB_START_RE.match(line)
    if nested is not None and nested.group(2) is not None:
        _walk_section(field, _join(path, nested.group(1)), nested.group(2), found, seen)
        return
    found.update(_sigma_params(field, path, line))


def _walk_file(field: str, path: str, found: dict[str, float], seen: set[Any]) -> None:
    """A whole ``.include``d file: its top-level sigma ``.param``s, plus the
    files it includes in turn. ``.subckt`` bodies are skipped -- a parameter
    declared there is subcircuit-local, not the deck-level sigma."""
    key = (os.path.abspath(path), None)
    if key in seen:
        return
    seen.add(key)
    if not os.path.isfile(path):
        raise SimError(
            f"{field} cannot read model library {path} (included by the "
            "selected mismatch section) on this host"
        )
    for line in _top_level(_logical_lines(_read(field, path))):
        include = _INCLUDE_RE.match(line)
        if include is not None:
            _walk_file(field, _join(path, include.group(1)), found, seen)
            continue
        found.update(_sigma_params(field, path, line))


def _join(from_path: str, ref: str) -> str:
    ref = ref.strip().strip("'\"")
    return ref if os.path.isabs(ref) else os.path.join(os.path.dirname(from_path), ref)


def _strip_comment(body: str) -> str:
    """``body`` up to its first ngspice ``$``/``;`` comment outside a quoted
    or braced term, so a commented-out sigma is neither scaled nor refused."""
    for match in _COMMENT_RE.finditer(body):
        if match.group(1):
            return body[: match.start(1)]
    return body


def _sigma_params(field: str, path: str, line: str) -> dict[str, float]:
    """The ``<name>_mm = <number>`` assignments of one ``.param`` card.

    A sigma whose value is **not** a plain numeric literal raises rather
    than being skipped (issue #5486): skipping it would scale its siblings
    and leave it at nominal -- a partially scaled run that
    ``mismatch_scale: 0`` no longer makes a sigma=0 control, and that nothing
    downstream flags, ``mismatch_params`` listing only what was written.
    """
    match = _PARAM_RE.match(line)
    if match is None:
        return {}
    out: dict[str, float] = {}
    for name, value in _ASSIGN_RE.findall(_strip_comment(match.group(1))):
        if not name.lower().endswith(SIGMA_SUFFIX):
            continue
        number = _spice_literal(value)
        if number is None:
            raise SimError(
                f"{field} cannot scale mismatch-sigma parameter {name!r}, "
                f"defined as {value.strip()} (not a plain numeric literal) in "
                f"{path} -- refusing rather than scaling only its siblings"
            )
        out[name] = number
    return out
