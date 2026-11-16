"""SPICE logical-line reading and ``.subckt`` parameter-declaration scanning
for ``klt sim``'s per-instance mismatch gate (issue #5487).

:mod:`klayout_tools.sim_mismatch_gate` gates an instance one of two ways. The
instance-parameter mode (ihp-sg13g2's ``mm_ok``) may only write its parameter
onto an instance whose **subcircuit definition declares it** -- ngspice accepts
the assignment silently on a subcircuit declaring some other parameter and
leaves the devices inside it randomised, so klt would otherwise report
``randomised: false`` for an instance that was still randomised. Deciding that
means *reading the definitions*, which is what this module does: the netlist
and its ``.include`` closure, plus the model libraries as the deck's ``.lib
<file> <section>`` cards actually pull them in.

Reading definitions, never a device-name list: a PDK pin bump that renames or
adds a device needs no change here. The ``.subckt``-line and body-``.param``
spellings both count, because ngspice honours an instance-line override of
either (``sg13_lv_nmos`` uses the first, ``cap_cmim`` the second).

The logical-line/token helpers live here too, as the lower layer both this
module and the gate's own instance-line rewriting read netlists with.
"""

from __future__ import annotations

import os
from typing import Any

from .sim_errors import SimError

#: Caps on the model-library walk :func:`scan_library_declared_params` does,
#: so a pathological ``.include`` tree cannot turn a refusal check into an
#: unbounded read. Mirrors the posture of the netlist closure's own limits.
_MAX_LIB_FILES = 128
_MAX_LIB_DEPTH = 16


def split_tokens(line: str) -> list[str]:
    """Whitespace split that keeps ``{...}`` and quoted expressions whole."""
    tokens: list[str] = []
    cur: list[str] = []
    depth = 0
    quote = ""
    for ch in line:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch.isspace() and depth == 0:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def split_eol_comment(line: str) -> tuple[str, str]:
    """Split one *physical* line into ``(code, comment)``.

    ngspice ends an element at an unquoted ``;``, at a ``//``, or at a ``$``
    that opens the line or follows whitespace, and treats the rest as a
    comment -- so nothing written after one is part of the element. Separating
    the two *before* parsing is what stops the gate appending ``<param>=0``
    inside a comment (ngspice would silently drop it, leaving the instance
    randomised while klt reported ``randomised: false``) and stops comment
    prose being read as a parameter assignment or a ``.lib`` argument.

    ``//`` needs no preceding whitespace, unlike ``$``: measured on ngspice-47
    (2026-11-13), ``X1 n1 0 rr//note mm_ok=0`` leaves ``mm_ok`` at its default
    just as the spaced spelling does.

    Quoted text and ``{...}`` expressions are respected, as in
    :func:`split_tokens`.
    """
    depth = 0
    quote = ""
    for index, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and (
            ch == ";"
            or (ch == "$" and (index == 0 or line[index - 1].isspace()))
            or (ch == "/" and line[index + 1 : index + 2] == "/")
        ):
            return line[:index], line[index:]
    return line, ""


#: One logical line: ``(first, last, code, comment, folded_over)``.
LogicalLine = tuple[int, int, str, str, list[str]]


def _dropped_before_fold(line: str, code: str, comment: str) -> bool:
    """Whether ngspice discards this *physical* line before it folds ``+``
    continuations, so a continuation after it still belongs to the element
    above: a blank line, a ``*`` line, or a line that is nothing but a ``$``
    or ``//`` comment (any indent). A line that is nothing but a ``;``
    comment is **not** dropped -- it *breaks* the element, and the ``+``
    after it is discarded with no warning, so klt must end the element there
    too. Measured on ngspice-47; the decks are in docs/cli/sim.md.

    Folding the wrong way round either attaches the continuation's parameters
    to a comment (leaving the element's own ``mm_ok`` untouched while klt
    reported ``randomised: false``) or claims parameters ngspice never
    applied.
    """
    if line.lstrip().startswith("*"):
        return True
    if code.strip():
        return False
    return not comment.lstrip().startswith(";")


def _fold(text: str, *, strip_comments: bool) -> list[LogicalLine]:
    """Fold ``+`` continuations as ngspice does, skipping the lines it drops
    first (:func:`_dropped_before_fold`).

    ``folded_over`` holds the dropped lines that sat *inside* a continuation
    run -- not part of the element, but handed back so a caller rewriting the
    logical line re-emits them instead of deleting the author's text. A
    dropped line outside a run is its own entry, copied through verbatim.
    """
    raw = text.split("\n")
    split = [split_eol_comment(line) for line in raw]
    skip = [_dropped_before_fold(ln, *sp) for ln, sp in zip(raw, split, strict=True)]
    parts = split if strip_comments else [(line, "") for line in raw]
    out: list[LogicalLine] = []
    i = 0
    while i < len(raw):
        code, comment = parts[i]
        comments = [comment.strip()] if comment.strip() else []
        folded: list[str] = []
        pending: list[int] = []
        last = i
        j = i + 1
        while not skip[i] and j < len(raw):
            if skip[j]:
                pending.append(j)
            elif raw[j].lstrip().startswith("+"):
                folded += [raw[k] for k in pending]
                pending = []
                code += " " + parts[j][0].lstrip()[1:]
                if parts[j][1].strip():
                    comments.append(parts[j][1].strip())
                last = j
            else:
                break
            j += 1
        out.append((i, last, code, " ".join(comments), folded))
        i = last + 1
    return out


def logical_lines_with_comments(text: str) -> list[LogicalLine]:
    """:func:`_fold` with every *physical* line's end-of-line comment
    separated out **before** the ``+`` continuations are folded.

    Folding first would splice a comment into the middle of the logical line,
    where it swallows the continuation's own tokens; separating first keeps
    the code contiguous and hands the caller the comment text, so a rewritten
    line can carry it along instead of dropping it.
    """
    return _fold(text, strip_comments=True)


def logical_lines(text: str) -> list[LogicalLine]:
    """:func:`_fold` keeping each line's comment text inline in ``code``
    (``comment`` is always empty), for the wrapper path, which moves the
    instance line verbatim and so needs no comment separated out."""
    return _fold(text, strip_comments=False)


def merge_equals(tokens: list[str]) -> list[str]:
    """Join the ``name = value`` / ``name= value`` / ``name =value`` spellings
    of a parameter assignment into one ``name=value`` token.

    SPICE accepts whitespace around ``=``; :func:`split_tokens` does not,
    so without this a spaced ``mm_ok = 0`` would read as three positional
    tokens -- enough to mis-identify the subcircuit name on an instance line,
    or to append a second, conflicting assignment. Applied only on the
    instance-parameter path (and when reading parameter *declarations*), so
    the wrapper path's byte-for-byte output is untouched.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "=" and out and i + 1 < len(tokens):
            out[-1] = out[-1] + "=" + tokens[i + 1]
            i += 2
        elif (
            token.endswith("=")
            and i + 1 < len(tokens)
            and not tokens[i + 1].startswith("=")
        ):
            out.append(token + tokens[i + 1])
            i += 2
        elif token.startswith("=") and out:
            out[-1] = out[-1] + token
            i += 1
        else:
            out.append(token)
            i += 1
    return out


def _assigned_names(tokens: list[str]) -> set[str]:
    """Lower-cased left-hand sides of the ``name=value`` tokens in ``tokens``."""
    return {
        t.split("=", 1)[0].strip().lower() for t in tokens if "=" in t and t[0] != "="
    }


def scan_declared_params(text: str, acc: dict[str, set[str]]) -> dict[str, set[str]]:
    """Record ``subckt name (lower-cased) -> declared parameter names`` from
    one SPICE file's ``text`` into ``acc``, and return it.

    A subcircuit declares a parameter either on its ``.subckt`` line
    (``.subckt sg13_lv_nmos d g s b`` + ``+ w=0.35u ... mm_ok=1 ...``) or with
    a ``.param`` inside its body (``cap_cmim`` does the latter) -- both count,
    because ngspice honours an instance-line override of either.

    Content inside a ``.lib <section>`` … ``.endl`` *definition* block is
    skipped: a plain ``.include`` of this file does not pull those in. Use
    :func:`scan_library_declared_params` to read a named section.
    """
    _scan(text, acc, section=None, base_dir=None, seen=None, budget=None, depth=0)
    return acc


def scan_library_declared_params(
    path: str, section: str | None, acc: dict[str, set[str]]
) -> dict[str, set[str]]:
    """Like :func:`scan_declared_params`, for the model library ``path`` as
    the deck's ``.lib <path> <section>`` card pulls it in: only the named
    section's content (and the ``.include``/``.lib`` closure it reaches) is
    scanned. Unreadable files are skipped rather than raised on -- a missing
    declaration refuses the instance downstream, which is the safe direction.
    """
    _scan_file(path, section, acc, seen=set(), budget=[_MAX_LIB_FILES], depth=0)
    return acc


def _scan_file(
    path: str,
    section: str | None,
    acc: dict[str, set[str]],
    *,
    seen: set[tuple[str, str]],
    budget: list[int],
    depth: int,
) -> None:
    if depth > _MAX_LIB_DEPTH or budget[0] <= 0:
        return
    real = os.path.realpath(path)
    key = (real, (section or "").lower())
    if key in seen:
        return
    seen.add(key)
    budget[0] -= 1
    try:
        with open(real, encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read()
    except OSError:
        return
    _scan(
        text,
        acc,
        section=section,
        base_dir=os.path.dirname(real),
        seen=seen,
        budget=budget,
        depth=depth,
    )


def _resolve_ref(target: str, base_dir: str | None) -> str | None:
    """A ``.include``/``.lib`` target as a readable absolute path, or ``None``."""
    cleaned = target.strip().strip("\"'")
    if not cleaned:
        return None
    cleaned = os.path.expanduser(os.path.expandvars(cleaned))
    if "$" in cleaned:
        return None
    if not os.path.isabs(cleaned):
        if base_dir is None:
            return None
        cleaned = os.path.join(base_dir, cleaned)
    return cleaned


def _scan(
    text: str,
    acc: dict[str, set[str]],
    *,
    section: str | None,
    base_dir: str | None,
    seen: set[tuple[str, str]] | None,
    budget: list[int] | None,
    depth: int,
) -> None:
    """Walk ``text``'s logical lines, recording parameter declarations from
    the regions a ``.lib <section>`` card (or a plain include, ``section is
    None``) would actually pull in."""
    lib_stack: list[bool] = []
    subckt_stack: list[str] = []
    refs = _Refs(acc, base_dir, seen, budget, depth)
    for _first, _last, joined, _comment, _folded in logical_lines_with_comments(text):
        stripped = joined.strip()
        low = stripped.lower()
        if low.startswith((".lib", ".endl")):
            _track_lib_line(stripped, low, lib_stack, section, refs)
            continue
        if not _is_active(lib_stack, section):
            continue
        if low.startswith((".include", ".inc ")):
            tokens = split_tokens(stripped)
            refs.follow(tokens[1] if len(tokens) >= 2 else None, None)
            continue
        _record_declaration(stripped, low, acc, subckt_stack)


class _Refs:
    """The ``.include`` / ``.lib <file> <section>`` recursion one :func:`_scan`
    pass may make, with its file budget and cycle set -- ``None`` when the
    caller asked for a text-only scan (:func:`scan_declared_params`)."""

    def __init__(
        self,
        acc: dict[str, set[str]],
        base_dir: str | None,
        seen: set[tuple[str, str]] | None,
        budget: list[int] | None,
        depth: int,
    ) -> None:
        self._acc = acc
        self._base_dir = base_dir
        self._seen = seen
        self._budget = budget
        self._depth = depth

    def follow(self, ref: str | None, into: str | None) -> None:
        if ref is None or self._seen is None or self._budget is None:
            return
        target = _resolve_ref(ref, self._base_dir)
        if target is not None:
            _scan_file(
                target,
                into,
                self._acc,
                seen=self._seen,
                budget=self._budget,
                depth=self._depth + 1,
            )


def _track_lib_line(
    stripped: str,
    low: str,
    lib_stack: list[bool],
    section: str | None,
    refs: _Refs,
) -> None:
    """Keep ``lib_stack`` tracking which ``.lib`` section definitions we are
    inside, and follow a file-bearing ``.lib <file> <section>`` reference."""
    if low.startswith(".endl"):
        if lib_stack:
            lib_stack.pop()
        return
    tokens = split_tokens(stripped)
    if len(tokens) >= 3:
        # `.lib <file> <section>` is a *reference*, not a definition.
        if _is_active(lib_stack, section):
            refs.follow(tokens[1], tokens[2])
        return
    name = tokens[1].lower() if len(tokens) >= 2 else ""
    lib_stack.append(section is not None and name == section.lower())


def _record_declaration(
    stripped: str, low: str, acc: dict[str, set[str]], subckt_stack: list[str]
) -> None:
    """Record one ``.subckt`` header's or body ``.param``'s declared parameter
    names, and keep ``subckt_stack`` tracking which definition we are in."""
    if low.startswith(".subckt"):
        tokens = merge_equals(split_tokens(stripped))
        name = tokens[1].lower() if len(tokens) >= 2 else ""
        subckt_stack.append(name)
        acc.setdefault(name, set()).update(_assigned_names(tokens[2:]))
    elif low.startswith(".ends"):
        if subckt_stack:
            subckt_stack.pop()
    elif low.startswith(".param") and subckt_stack:
        tokens = merge_equals(split_tokens(stripped))
        acc.setdefault(subckt_stack[-1], set()).update(_assigned_names(tokens[1:]))


def _is_active(lib_stack: list[bool], section: str | None) -> bool:
    """Whether the current region is one the deck's ``.lib`` card pulls in."""
    if not lib_stack:
        return section is None
    return all(lib_stack)


def collect(
    netlist_path: str,
    instance_param: str | None,
    corner_points: list[Any],
    models_lib_ref: str | None,
    resolve: Any,
) -> dict[str, set[str]]:
    """Every subcircuit parameter declaration this run's decks can see, or
    ``{}`` when no ``instance_param`` mode is in play (the wrapper mode needs
    no definitions, and must keep costing nothing extra).

    Two sources, matching what ngspice is handed: the request netlist with its
    ``.include`` closure, and -- for each ``(library, section)`` pair the
    corner points' ``.lib`` cards name (:meth:`CornerPoint.section_lib`) --
    only that section's content. ``resolve`` maps a declared library ref to an
    absolute path (or ``None`` when it does not resolve: an unreadable library
    contributes no declarations, which refuses the affected instance
    downstream rather than turning a gating request into a different error).
    """
    declared: dict[str, set[str]] = {}
    if instance_param is None:
        return declared
    scan_library_declared_params(netlist_path, None, declared)
    models_lib = resolve(models_lib_ref)
    for lib, section in library_sections(corner_points, models_lib, resolve):
        scan_library_declared_params(lib, section, declared)
    return declared


def library_sections(
    corner_points: list[Any], models_lib: str | None, resolve: Any
) -> list[tuple[str, str]]:
    """The distinct ``(library path, section)`` pairs this run's decks pull in
    -- the same ``.lib <path> <section>`` cards ``sim_param_file`` writes."""
    pairs: list[tuple[str, str]] = []
    for point in corner_points:
        sections = (
            point.process_sections
            if point.process_sections is not None
            else ([point.process] if point.process else [])
        )
        for index, section in enumerate(sections):
            ref = point.section_lib(index, models_lib)
            if not ref or not section:
                continue
            resolved = resolve(ref)
            if resolved and (resolved, section) not in pairs:
                pairs.append((resolved, section))
    return pairs


def gate_context(
    models: dict[str, Any], request_dir: str, points: list[Any], resolve_lib_ref: Any
) -> tuple[Any, ...]:
    """The trailing :func:`collect` arguments ``sim`` hands the gate (#5487).

    ``resolve_lib_ref`` is ``sim``'s own ``models.lib`` resolution rule,
    injected rather than reimplemented so a library ref resolves here exactly
    as it will when the deck is written. It is wrapped to yield ``None``
    instead of raising: an unresolvable library simply contributes no
    declarations -- which refuses the affected instance, the safe direction --
    rather than turning a gating request into a model-resolution error.

    ``models`` is used as-is: :func:`sim._model_resolution_models`' staged-ref
    view cannot apply here, because a staged worker request is off-host and
    ``monte_carlo.instances`` is refused on off-host backends.
    """

    def resolve(ref: str | None) -> str | None:
        try:
            return resolve_lib_ref(ref, models, request_dir) if ref else None
        except SimError:
            return None

    return (points, models.get("lib"), resolve)
