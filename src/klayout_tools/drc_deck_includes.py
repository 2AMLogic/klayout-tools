"""Static include-graph walk for a KLayout DRC deck (issues #2333, #5443).

Split out of :mod:`klayout_tools.drc_klayout_engine` so neither file carries
the other's debt: that module is a ``klayout`` subprocess wrapper, this one is
pure text-and-filesystem analysis that needs no KLayout at all.

A deck's tree identity, its ``-rd`` globals scan and its host-command
preflight all have to answer the same question first -- *which files is this
deck actually made of?* -- and a PDK's native deck answers it two ways: Ruby's
literal ``require``/``require_relative``/``load`` (issue #2333), and KLayout's
own ``# %include`` preprocessor lines (issue #5443). Everything here is
best-effort and static: an include it cannot resolve to a readable file is
skipped, never guessed at and never an error.
"""

from __future__ import annotations

import os
import re

#: Ruby's literal-path include forms, so a driver split across files is
#: scanned as a whole (issue #2333). Only string *literals* are followed --
#: a computed path is unknowable statically and is simply not scanned.
_RUBY_INCLUDE_RE = re.compile(
    r"""\b(?:require_relative|require|load)\s*\(?\s*"""
    r"""(?:"(?P<path_dq>[^"\n]*)"|'(?P<path_sq>[^'\n]*)')"""
)

#: KLayout's own preprocessor include (issue #5443). ``# %include <path>``
#: comment lines are spliced in textually before the deck is interpreted, so
#: for a PDK whose native deck is a thin driver over a directory of rule
#: files, essentially every rule arrives this way.
#:
#: Shaped after ``tl::IncludeExpander``, verified line form by line form
#: against KLayout 0.30.12: the ``#`` must be the line's first non-blank
#: character (``## %include`` and a trailing ``code # %include`` are both
#: inert), only blanks may sit between it and a case-sensitive ``%include``,
#: no blank is needed *after* the keyword (``# %includea.drc`` includes
#: ``a.drc``), and the path is the remainder of the line -- there is no
#: trailing-comment syntax.
#:
#: Two deliberate divergences remain, so this is a close match rather than an
#: exact one:
#:
#: * ``# %include $("dir")/a.drc`` -- KLayout interpolates ``$(...)``
#:   expressions before resolving the path. A computed path is unknowable
#:   statically, so it is skipped (as issue #5443 specified) and the file it
#:   names is *not* covered by the deck tree hash.
#: * ``# %include "a.drc" trailing`` -- KLayout parses the quoted path and
#:   then rejects the leftover text, aborting the whole deck (``Expected end
#:   of text here: trailing in MacroInterpreter::include_expansion``);
#:   :func:`_klayout_include_path` drops the leftover and the walker follows
#:   the include. It over-includes only for a deck KLayout refuses to run at
#:   all, so it is left as is.
_KLAYOUT_INCLUDE_RE = re.compile(
    r"^[ \t]*#[ \t]*%include[ \t]*(?P<path>.+)$", re.MULTILINE
)

#: Upper bound on how many files one deck's include graph is scanned for
#: (issue #2333) -- a static scan must never become the expensive part of a
#: DRC run, and a driver that pulls in more than this is past the point where
#: a heuristic preflight is useful.
_DECK_SCAN_MAX_FILES = 64


def _klayout_include_path(raw: str) -> str:
    """The path a ``# %include`` line names, de-quoted as KLayout de-quotes it
    (issue #5443).

    An opening ``"``/``'`` starts a quoted path that ends at the next matching
    quote, or at the end of the line if there is none; anything else is taken
    verbatim. Both halves were verified against KLayout 0.30.12, including the
    asymmetry: ``"a/t.rb`` resolves to ``a/t.rb`` while ``a/t.rb"`` keeps its
    trailing quote and fails to open. Content *after* a closing quote is
    dropped rather than rejected -- see :data:`_KLAYOUT_INCLUDE_RE`.
    """
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        end = raw.find(raw[0], 1)
        return raw[1:] if end < 0 else raw[1:end]
    return raw


def _resolve_include(raw: str, base: str, suffixes: tuple[str, ...]) -> str | None:
    """``raw`` as an absolute path to an existing file, resolved against
    ``base`` (the including file's directory) and the first of ``suffixes``
    that exists; ``None`` when it names nothing readable."""
    if not raw:
        return None
    candidate = raw if os.path.isabs(raw) else os.path.join(base, raw)
    for resolved in (candidate + suffix for suffix in suffixes):
        if os.path.isfile(resolved):
            return os.path.abspath(resolved)
    return None


def _deck_include_targets(text: str, base: str) -> list[str]:
    """Every existing file ``text`` includes, in source order per form:
    Ruby literal ``require``/``require_relative``/``load`` (issue #2333),
    then KLayout ``# %include`` (issue #5443). Both resolve relative to
    ``base``, the directory of the file ``text`` came from.
    """
    targets: list[str] = []
    for match in _RUBY_INCLUDE_RE.finditer(text):
        raw = match.group("path_dq") or match.group("path_sq") or ""
        if "#{" in raw:
            # A computed path is unknowable statically; skip, never guess.
            continue
        # Ruby resolves a suffixless `require` name against `<name>.rb`.
        found = _resolve_include(raw, base, ("", ".rb"))
        if found is not None:
            targets.append(found)
    for match in _KLAYOUT_INCLUDE_RE.finditer(text):
        # No suffix search: KLayout splices the named path exactly and errors
        # out when it does not exist, so a path that resolves only with a
        # suffix added is not part of the deck. A `$(...)`-interpolated path
        # names no file literally and so falls out here, unfollowed.
        raw = _klayout_include_path(match.group("path"))
        found = _resolve_include(raw, base, ("",))
        if found is not None:
            targets.append(found)
    return targets


def _deck_script_files(deck_file: str) -> list[str]:
    """``deck_file`` plus every literal-path include reachable from it --
    Ruby ``require``/``require_relative``/``load`` (issue #2333) and KLayout
    ``# %include`` (issue #5443) -- breadth-first, de-duplicated, bounded by
    :data:`_DECK_SCAN_MAX_FILES`.

    A PDK's driver is often one file, but the shapes that carry host
    assumptions (a shared logging/util prologue, a per-topic rule table) split
    it across a handful, and a ``%include``-based deck keeps its whole rule
    set out of the entry script. Both include forms resolve relative to the
    *including* file. Unreadable or computed-path includes are skipped, not
    an error: this is a diagnostic aid, and a file it cannot read is simply
    one it cannot make claims about.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    pending = [os.path.abspath(deck_file)]
    while pending and len(ordered) < _DECK_SCAN_MAX_FILES:
        current = pending.pop(0)
        if current in seen or not os.path.isfile(current):
            continue
        seen.add(current)
        ordered.append(current)
        try:
            with open(current, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        pending.extend(_deck_include_targets(text, os.path.dirname(current)))
    return ordered
