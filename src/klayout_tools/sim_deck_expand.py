"""Runner-side ``$VAR``/``~`` expansion for `klt sim` deck units (issue #4881).

ngspice expands neither environment variables nor ``~`` in ``.include``,
``.lib <file> <section>`` or ``pre_osdi`` targets, so a deck naming a
runner-resident file as ``$PDK_ROOT/...`` is expanded here, on the
executing host, just before ngspice launches. The shipped deck bytes (and
their recorded ``deck_sha256``) are never modified.

``sim_decks`` imports this module; ``sim.SimError`` is imported inside the
functions that use it (no load-time cycle).
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from . import sim_staging

#: A ``pre_osdi <file>`` control command, classified like an include
#: target. The staged file is a binary shared library, so it is shipped but
#: never scanned for further directives.
_PRE_OSDI_RE = re.compile(r"^\s*pre_osdi\s+(?P<target>\S.*?)\s*$", re.IGNORECASE)

_EXPANDED_DECK_PREFIX = ".klt-expanded-"


def pre_osdi_target(line: str) -> str | None:
    """The (unquoted) file a ``pre_osdi`` line names, else ``None``."""
    match = _PRE_OSDI_RE.match(line)
    return None if match is None else sim_staging._unquote(match.group("target"))


def closure_directive_target(line: str, *, where: str) -> tuple[str, str] | None:
    """The file a deck line references, as ``(directive, target)``, or
    ``None`` for a line that names no file.

    ``.include``/``.inc`` always name one. ``.lib`` is two-faced (the same
    split ``sim_staging._Stager._parse_lib`` makes, #2668): one token is a
    section *definition* (``.lib tt`` ... ``.endl``) and names no file; two
    tokens are a file-bearing ``.lib <file> <section>`` reference -- the
    model-reference form PDK corner selection uses -- whose file must be
    staged, left to the executing host, or rejected exactly like an include
    target. Any other ``.lib`` shape is rejected by name rather than
    guessed at, so it cannot slip past the pre-launch check."""
    from .sim import SimError

    match = sim_staging._INCLUDE_RE.match(line)
    if match is not None:
        return "`.include`", sim_staging._unquote(match.group("target"))
    osdi = pre_osdi_target(line)
    if osdi is not None:
        return "`pre_osdi`", osdi
    lib_match = sim_staging._LIB_RE.match(line)
    if lib_match is None:
        return None
    try:
        tokens = shlex.split(lib_match.group("rest"))
    except ValueError as exc:
        tokens = []
        problem = str(exc)
    else:
        problem = f"{len(tokens)} argument(s)"
    if len(tokens) == 1:
        return None
    if len(tokens) == 2:
        return "`.lib`", tokens[0]
    raise SimError(
        f"{where}: unsupported `.lib` directive form ({problem}) -- deck-list "
        "requests understand `.lib <section>` definitions and "
        "`.lib <file> <section>` references only"
    )


def expand_directive_line(line: str) -> str:
    """Expand ``$VAR``/``~`` in one ``.include``/``.inc``/``.lib <file>
    <section>``/``pre_osdi`` target on the executing host. A line with
    nothing to expand, or whose variable is unset (the engine then reports
    it), is returned as is."""
    from .sim import SimError

    ending = line[len(line.rstrip("\r\n")) :]
    body = line[: len(line) - len(ending)]
    try:
        found = closure_directive_target(body, where="")
    except SimError:  # malformed shapes are classified pre-launch
        return line
    if found is None:
        return line
    target = found[1]
    if "$" not in target and not target.startswith("~"):
        return line
    expanded = os.path.expanduser(os.path.expandvars(target))
    if "$" in expanded or not expanded.strip():
        return line
    index = body.find(target)
    if index < 0:
        return line
    if any(ch.isspace() for ch in expanded):
        expanded = f'"{expanded}"'
        # the original may already be quoted: swallow its quotes
        if body[index - 1 : index] in "\"'" and body[index - 1 : index]:
            index -= 1
            target = body[index] + target + body[index]
    return body[:index] + expanded + body[index + len(target) :] + ending


def expand_text(text: str) -> str:
    if "$" not in text and "~" not in text:
        return text
    return "".join(expand_directive_line(ln) for ln in text.splitlines(keepends=True))


def prepare_run_deck(unit: dict[str, Any], *, unit_dir: str) -> str:
    """The deck name ngspice is launched with. When the deck (or a staged
    include) names a runner-resident file through ``$VAR``/``~``, the
    executing host expands it here: staged includes in place (the resume
    key hashes the staging root, not these bytes) and the deck itself into
    a sidecar copy, so the shipped deck -- and its recorded ``deck_sha256``
    -- stay byte-for-byte as submitted."""
    for ref in unit["includes"]:
        path = os.path.join(unit_dir, ref["name"])
        if not ref["name"].lower().endswith(".osdi"):
            rewrite_if_expanded(path, path)
    deck_name = unit["deck"]["name"]
    deck_path = os.path.join(unit_dir, deck_name)
    head, tail = os.path.split(deck_name)
    run_name = os.path.join(head, _EXPANDED_DECK_PREFIX + tail)
    if rewrite_if_expanded(deck_path, os.path.join(unit_dir, run_name)):
        return run_name
    return deck_name


def rewrite_if_expanded(source: str, dest: str) -> bool:
    try:
        with open(source, "rb") as handle:
            raw = handle.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    expanded = expand_text(text)
    if expanded == text:
        return False
    with open(dest, "wb") as handle:
        handle.write(expanded.encode("utf-8"))
    return True
