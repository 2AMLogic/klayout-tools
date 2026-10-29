"""Replace a named ``.subckt`` of a netlist's ``.include`` closure (issue #3919).

ngspice keeps the **first** of two same-named ``.subckt`` definitions, so a
caller who wants a reduced model of a hierarchical block (a large array, say)
cannot simply add a second definition. ``request.netlist_overrides.subckt``
names the subcircuit and the file holding its replacement; this module

1. resolves the netlist's ``.include``/``.inc`` closure with the same resolver
   the off-host backends stage with (:func:`sim_staging.stage_netlist`),
2. finds the one file defining ``.subckt <name>`` (case-insensitive),
3. writes a copy of it into a per-run directory with only that
   ``.subckt`` ... ``.ends`` block removed and ``.include <replacement>`` in
   its place, everything else byte-identical, and
4. re-points the ``.include`` chain from the netlist down to that file at the
   copies.

The caller's own files are never edited. Matching is lexical (``.subckt`` /
``.ends`` with nesting depth), not a SPICE parse. Includes left to the
executing host (``$VAR`` targets, PDK-root files) are not scanned.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import sim_staging
from ._provenance import sha256_file

_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(?P<name>\S+)", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
_INCLUDE_RE = re.compile(
    r"^(?P<lead>\s*\.inc(?:lude)?\s+)(?P<target>\"[^\"]*\"|'[^']*'|\S+)(?P<tail>.*)$",
    re.IGNORECASE,
)


class OverrideError(ValueError):
    """Invalid or unresolvable ``netlist_overrides`` request."""


@dataclass(frozen=True)
class SubcktOverride:
    name: str
    replacement: str  # absolute path


def parse_spec(spec: Any, request_dir: str) -> list[SubcktOverride]:
    """Validate ``request.netlist_overrides`` into overrides, resolving each
    ``replacement`` against ``request_dir``."""
    if not isinstance(spec, dict):
        raise OverrideError("request.netlist_overrides must be an object")
    unknown = sorted(set(spec) - {"subckt"})
    if unknown:
        raise OverrideError(
            f"request.netlist_overrides has unsupported key(s) {unknown}; "
            "only 'subckt' is supported"
        )
    entries = spec.get("subckt")
    if not isinstance(entries, list) or not entries:
        raise OverrideError(
            "request.netlist_overrides.subckt must be a non-empty list of "
            '{"name": ..., "replacement": ...} objects'
        )
    out: list[SubcktOverride] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"request.netlist_overrides.subckt[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"name", "replacement"}:
            raise OverrideError(
                f'{where} must be an object with exactly "name" and "replacement"'
            )
        name, replacement = entry["name"], entry["replacement"]
        if not isinstance(name, str) or not name.strip() or len(name.split()) != 1:
            raise OverrideError(f"{where}.name must be a single subcircuit name")
        if not isinstance(replacement, str) or not replacement:
            raise OverrideError(f"{where}.replacement must be a file path string")
        if name.lower() in seen:
            raise OverrideError(f"{where}: subcircuit {name!r} is overridden twice")
        seen.add(name.lower())
        path = os.path.abspath(_resolve(replacement, request_dir))
        if not os.path.isfile(path):
            raise OverrideError(f"{where}.replacement not found: {path}")
        out.append(SubcktOverride(name=name, replacement=path))
    return out


def _resolve(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    return expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OverrideError(
            f"cannot read netlist file '{path}' as UTF-8: {exc}"
        ) from exc


def find_blocks(text: str, name: str) -> list[tuple[int, int]]:
    """``(first, last)`` line indices (inclusive) of each ``.subckt <name>``
    ... matching ``.ends`` block in ``text``. Nested ``.subckt`` definitions
    are tracked by depth; an unterminated block is an error."""
    lines = text.splitlines()
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    depth = 0
    for index, line in enumerate(lines):
        opened = _SUBCKT_RE.match(line)
        if opened:
            if start is None and opened.group("name").lower() == name.lower():
                start, depth = index, 0
            if start is not None:
                depth += 1
        elif start is not None and _ENDS_RE.match(line):
            depth -= 1
            if depth == 0:
                blocks.append((start, index))
                start = None
    if start is not None:
        raise OverrideError(
            f".subckt {name} starting at line {start + 1} has no matching .ends"
        )
    return blocks


def _strip(text: str, block: tuple[int, int], replacement_line: str) -> str:
    lines = text.splitlines(keepends=True)
    first, last = block
    ending = "\n"
    if lines[first].endswith("\r\n"):
        ending = "\r\n"
    return "".join(lines[:first] + [replacement_line + ending] + lines[last + 1 :])


def _include_targets(
    text: str, origin: str, roots: tuple[str, ...]
) -> list[tuple[int, str]]:
    """``(line_index, absolute_path)`` for each include this host resolves."""
    found: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines()):
        match = _INCLUDE_RE.match(line)
        if match is None:
            continue
        raw = match.group("target")
        target = raw[1:-1] if raw[0] in "\"'" else raw
        resolved = sim_staging.resolve_include_target(
            target, origin=origin, roots=roots
        )
        if resolved is not None and os.path.isfile(resolved):
            found.append((index, resolved))
    return found


def _quote(path: str) -> str:
    return f'"{path}"'


def _locate(
    item: SubcktOverride, files: list[str], current: dict[str, str]
) -> tuple[str, tuple[int, int]]:
    """The one ``(file, block)`` defining ``item.name``; raises for none/many or
    a replacement that does not define it."""
    hits = [
        (path, blocks)
        for path in files
        if (blocks := find_blocks(current[path], item.name))
    ]
    count = sum(len(blocks) for _, blocks in hits)
    if count == 0:
        raise OverrideError(
            f".subckt {item.name} is not defined in the netlist or its "
            "resolved .include closure (includes under $VAR or the PDK "
            "root are left to the executing host and are not searched)"
        )
    if count > 1:
        where = ", ".join(path for path, _ in hits)
        raise OverrideError(
            f".subckt {item.name} is ambiguous: {count} definitions in {where}"
        )
    if not find_blocks(_read(item.replacement), item.name):
        raise OverrideError(
            f"replacement '{item.replacement}' does not define .subckt {item.name}"
        )
    return hits[0][0], hits[0][1][0]


def _collect_edits(
    files: list[str],
    texts: dict[str, str],
    overrides: list[SubcktOverride],
    *,
    repo_root: str | None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """path -> new text (each override edits the possibly already edited file),
    plus the report entries (``stripped_sha256`` filled in later)."""
    from . import env_provenance

    current = dict(texts)
    edited: set[str] = set()
    report: list[dict[str, Any]] = []
    for item in overrides:
        path, block = _locate(item, files, current)
        current[path] = _strip(
            current[path], block, f".include {_quote(item.replacement)}"
        )
        edited.add(path)
        report.append(
            {
                "name": item.name,
                "source": env_provenance.repo_relative_path(path, repo_root=repo_root),
                "replacement": env_provenance.repo_relative_path(
                    item.replacement, repo_root=repo_root
                ),
                "original_sha256": sha256_file(path),
                "stripped_sha256": None,  # filled once the copy is written
                "replacement_sha256": sha256_file(item.replacement),
                "_path": path,
            }
        )
    return {path: current[path] for path in edited}, report


def _changed_closure(
    files: list[str],
    texts: dict[str, str],
    edits: dict[str, str],
    roots: tuple[str, ...],
) -> set[str]:
    """Edited files plus every ancestor in the include graph (to re-point)."""
    graph = {path: _include_targets(texts[path], path, roots) for path in files}
    changed = set(edits)
    grew = True
    while grew:
        grew = False
        for path, targets in graph.items():
            if path not in changed and any(t in changed for _, t in targets):
                changed.add(path)
                grew = True
    return changed


def _repoint(
    text: str, origin: str, copy_of: dict[str, str], roots: tuple[str, ...]
) -> str:
    """Absolutize each resolvable include in ``text``; edited children point at
    their copies. Positions are taken from the edited text (a block removal
    shifts lines)."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = _INCLUDE_RE.match(body)
        if match is None:
            continue
        raw = match.group("target")
        target = raw[1:-1] if raw[0] in "\"'" else raw
        resolved = sim_staging.resolve_include_target(
            target, origin=origin, roots=roots
        )
        if resolved is None or not os.path.isfile(resolved):
            continue
        resolved = copy_of.get(resolved, resolved)
        lines[index] = (
            f"{match.group('lead')}{_quote(resolved)}{match.group('tail')}"
            + line[len(body) :]
        )
    return "".join(lines)


def apply_overrides(
    netlist_path: str,
    overrides: list[SubcktOverride],
    *,
    work_dir: str,
    roots: tuple[str, ...] = (),
    repo_root: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(netlist_to_dispatch, report_entries)``; raises
    :class:`OverrideError` before anything is written for an unknown or
    ambiguous name or a replacement that does not define the subcircuit."""
    try:
        staged = sim_staging.stage_netlist(
            netlist_path, netlist_staged_name="netlist.cir", roots=roots
        )
    except sim_staging.IncludeStagingError as exc:
        raise OverrideError(str(exc)) from exc
    files = [os.path.abspath(f.source_path or f.local_path or "") for f in staged.files]
    texts = {path: _read(path) for path in files}

    edits, report = _collect_edits(files, texts, overrides, repo_root=repo_root)
    changed = _changed_closure(files, texts, edits, roots)
    copy_of = {
        path: os.path.join(work_dir, f"{index}_{os.path.basename(path)}")
        for index, path in enumerate(sorted(changed))
    }
    # The top-level netlist keeps a stable name for ngspice's diagnostics.
    top = os.path.abspath(netlist_path)
    if top in copy_of:
        copy_of[top] = os.path.join(work_dir, "netlist.subckt-override.cir")
    for path in sorted(changed):
        text = _repoint(edits.get(path, texts[path]), path, copy_of, roots)
        with open(copy_of[path], "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    for entry in report:
        entry["stripped_sha256"] = sha256_file(copy_of[entry.pop("_path")])
    return copy_of.get(top, top), report
