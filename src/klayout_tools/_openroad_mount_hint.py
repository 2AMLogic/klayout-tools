"""The mount-namespace engine-error hint shared by every OpenROAD-driving
``klt`` verb (issues #1868/#5753).

``mount_namespace_hint``/``outside_cwd_note`` and their two regexes lived
in ``place_and_route.py``/``place_and_route_hints.py`` until issue #5753
asked for one shared helper both verbs import so they cannot drift; they
sit in this own module rather than ``_openroad_engine.py`` to keep that
module at its file-size ceiling (the ratchet, issue #2766), the same
precedent as ``place_and_route_hints.py`` (issue #4914).
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._openroad_engine import _OpenRoadResult

# --------------------------------------------------------------------------- #
# Mount-namespace hint, shared by every OpenROAD-driving verb (issue #1868)
# --------------------------------------------------------------------------- #

#: Matches the "can't read/open a file" *phrase* in an OpenROAD/Tcl error
#: line, deliberately loose on wording -- issue #1868 has observed both
#: ``cannot read file <path>.`` and ``couldn't open "<path>": ...``, and
#: OpenROAD's own diagnostics interpose extra words (``cannot open LEF file
#: <path>``). Matched against one line at a time; :data:`_ABS_PATH_RE` then
#: pulls the actual path out of a line this matches, rather than trying to
#: encode both the phrase and the path shape in one pattern.
_UNREADABLE_FILE_PHRASE_RE = re.compile(
    r"(?:cannot|can[' ]?t|couldn[' ]?t|could\s+not)\s+(?:read|open)\b",
    re.IGNORECASE,
)

#: The first absolute-path-shaped token on a line -- used only after
#: :data:`_UNREADABLE_FILE_PHRASE_RE` has already matched that line, so a
#: bare ``/`` prefix is a reliable enough signal without also anchoring the
#: surrounding phrase.
_ABS_PATH_RE = re.compile(r"\"?(/[^\s\"]+)")


def outside_cwd_note(path: str) -> str:
    """Issue #4191: the docker wrapper mounts only `$PWD`, `$PDK_ROOT` and
    `$TMPDIR`, so a readable path outside the invocation directory is the
    commonest trigger -- say the rule and the fix, not just the symptom."""
    cwd = os.path.realpath(os.getcwd())
    real = os.path.realpath(path)
    if real == cwd or real.startswith(cwd.rstrip(os.sep) + os.sep):
        return ""
    return (
        f"; '{path}' is outside the invocation directory '{cwd}', and the "
        "container wrapper mounts only $PWD, $PDK_ROOT and $TMPDIR -- run "
        "from a common ancestor of the request and its inputs"
    )


def mount_namespace_hint(
    completed: _OpenRoadResult, pdk_info: dict[str, Any] | None
) -> str | None:
    """``None``, or an actionable hint that ``openroad`` and this process do
    not share a filesystem view (issue #1868).

    Every OpenROAD-driving ``klt`` verb resolves its input paths on the
    **host** (liberty/LEF via :mod:`klayout_tools.pdk`'s ``find_pdk``, a
    request-supplied netlist/SPEF/LEF by its request-given path) and bakes
    the resulting absolute paths into the generated Tcl handed to an
    ``openroad`` **subprocess** -- which, per the repo's own documented,
    CI-used install path, is very often actually a wrapper script that runs
    a container per invocation (``scripts/install-openroad-docker.sh``). A
    container only sees the host paths its wrapper explicitly bind-mounted,
    so a PDK discovered via a search root (``~/.volare``, ciel, open_pdks)
    rather than an explicitly-set ``$PDK_ROOT``, or a request directory
    outside the invocation directory, silently falls outside that wrapper's
    mount set -- see that script's own header comment.

    The result is an OpenROAD/Tcl "can't read/open file <path>" failure
    that, read alone, looks identical to a genuinely missing or unreadable
    file. The cheap, general signal that distinguishes the two: **this**
    process can still read that exact path (it is the one that resolved it
    in the first place). When that holds, the failure is a mount/namespace
    gap, not a missing file -- worth saying so, and worth surfacing the
    resolved PDK root/``resolved_via`` alongside it when the path is a PDK
    asset, so a reader sees ``resolved_via`` next to the unreadable path
    immediately rather than having to separately run ``klt pdk find``.

    This generalizes beyond PDK assets on purpose (issue #1868's own closing
    note): any absolute path outside ``$PWD``/``$PDK_ROOT`` that a request
    can reference -- an out-of-tree netlist, an ``--abstract-cell-lef``, a
    macro LEF given by absolute path -- hits the identical hazard, and
    ``pdk_info`` being ``None`` (or the path not living under its root) just
    means the hint is generic rather than PDK-specific.

    Like :func:`container_runtime_hint`, this is safe to *append* to an
    existing engine-error message rather than replace it: the engine's own
    captured diagnosis is still the primary evidence, the hint only says
    what that diagnosis means and what to do next. Shared here rather than
    kept per-verb so ``klt place-and-route`` and ``klt sta`` cannot drift
    (issue #5753).
    """
    path: str | None = None
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            if not _UNREADABLE_FILE_PHRASE_RE.search(line):
                continue
            path_match = _ABS_PATH_RE.search(line)
            if path_match is not None:
                path = path_match.group(1).rstrip(".,;:'\"")
                break
        if path is not None:
            break
    if path is None:
        return None

    try:
        readable = os.path.isfile(path) and os.access(path, os.R_OK)
    except OSError:
        readable = False
    if not readable:
        return None

    pdk_note = ""
    if pdk_info is not None and path.startswith(pdk_info.get("root", "\0")):
        pdk_note = (
            f" (resolved PDK root: '{pdk_info['root']}', "
            f"{pdk_info['resolved_via']}; $PDK_ROOT="
            f"{os.environ.get('PDK_ROOT') or '(unset)'})"
        )

    mount_note = outside_cwd_note(path)

    return (
        f"'openroad' could not read '{path}', but this process can read it"
        f"{pdk_note} -- if 'openroad' is a container/wrapper invocation, its "
        "mounts likely do not cover that path (see docs/cli/place-and-route.md's "
        f"'Installing OpenROAD' section){mount_note}"
    )
