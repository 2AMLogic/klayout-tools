"""Git-index facts a grading render needs about the tree it runs in.

Issue #5857 made the git index -- not the producing host's working tree --
the grading basis for ``klt signoff``'s input-artifact verification. This
module owns that predicate, :func:`is_untracked_working_tree_file`, plus
the memoized bulk ``git ls-files`` machinery behind it. Before it, a
gitignored scratch artifact cited by an envelope verified ``True`` on the
host that happened to have the file and ``None`` in a fresh clone, moving
``input_verified`` and, through it, the artifact-anchored T1 verdicts
between hosts; with it, an evidence render is a function of the commit.

The never-raise contract matches
:func:`klayout_tools.build_identity._git`: git missing from ``$PATH``, a
``.git`` entry with no functioning repository behind it, a command that
does not finish promptly -- each degrades to "not a functioning checkout
here", and callers keep their filesystem-presence behaviour. A tree with
no VCS metadata at all (a ``.git``-less archive unpack, a vendored copy, a
``--repo-root`` grading, issue #2878) has no index to ask, so it keeps
that behaviour everywhere.

:func:`repo_scoped_candidates` is the other half of the same choke point:
the repo-root discovery for a ``{path, scope: "repo"}`` input-artifact
value, extracted from ``signoff._resolve_input_artifact_value`` so the
resolution convention and the index-basis filter cannot drift apart.

Split out of ``_provenance.py`` in the same issue: that module is at its
trusted file-size ceiling, and first-party growth must land in files under
the size ratchet's hard limit (issue #2766).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .env_provenance import find_repo_root

#: Memoized ``git ls-files`` listing per checkout root, keyed by the root's
#: real path *and* its git index's stat signature -- so a mid-process
#: ``git add`` (tests do this between two grades) invalidates exactly that
#: root's entry while an unchanged index is answered from memory. One render
#: is one process, which is what makes this the "one ``ls-files`` per
#: distinct root per render" budget issue #5857 sets for ``klt signoff``'s
#: citations, however many of them name artifacts under the same root.
_TRACKED_FILES_CACHE: dict[tuple[str, int, int, int], frozenset[str] | None] = {}


def _git_stdout(cwd: str, *args: str) -> bytes | None:
    """Stdout of ``git -C <cwd> <args>``, or ``None`` when git is missing
    from ``$PATH``, the command fails, or it does not finish promptly.
    Never raises -- the trackedness probe below must not be able to break a
    render (:func:`klayout_tools.build_identity._git` is the precedent for
    this never-raise contract)."""
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _git_index_signature(root: str) -> tuple[int, int, int] | None:
    """``(st_ino, st_size, st_mtime_ns)`` of the git index behind ``root``
    -- the file a ``git add`` rewrites -- or ``None`` when there is no
    index to stat. A ``.git`` directory names its index directly; a
    ``.git`` *file* (a linked worktree) points at the gitdir holding it."""
    entry = os.path.join(root, ".git")
    try:
        if os.path.isdir(entry):
            index = os.path.join(entry, "index")
        else:
            with open(entry, encoding="utf-8", errors="replace") as handle:
                pointer = handle.readline().strip()
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = pointer[len("gitdir:") :].strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.join(root, gitdir)
            index = os.path.join(gitdir, "index")
        stat = os.stat(index)
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def _tracked_files_under(root: str) -> frozenset[str] | None:
    """The set of POSIX ``root``-relative paths git's index tracks, or
    ``None`` when ``root`` is not a functioning checkout of its own.

    Memoized per root (see :data:`_TRACKED_FILES_CACHE`). ``git ls-files``
    searches upwards when its ``-C`` directory is not itself a repository,
    so ``rev-parse --show-toplevel`` must name ``root`` back before the
    listing is believed: anything else (a ``.git``-marked directory with no
    repository behind it) is "not a checkout here" -- never the parent
    repository's paths re-keyed against ``root``."""
    signature = _git_index_signature(root)
    if signature is None:
        return None
    real_root = os.path.realpath(root)
    key = (real_root, *signature)
    if key not in _TRACKED_FILES_CACHE:
        top = _git_stdout(root, "rev-parse", "--show-toplevel")
        listing = (
            _git_stdout(root, "ls-files", "-z")
            if top is not None
            and os.path.realpath(top.decode("utf-8", errors="surrogateescape").strip())
            == real_root
            else None
        )
        _TRACKED_FILES_CACHE[key] = (
            frozenset(
                item.decode("utf-8", errors="surrogateescape")
                for item in listing.split(b"\0")
                if item
            )
            if listing is not None
            else None
        )
    return _TRACKED_FILES_CACHE[key]


def is_untracked_working_tree_file(path: str) -> bool:
    """Whether ``path`` names a file inside a functioning git checkout
    whose index does not track it (issue #5857) -- gitignored scratch, a
    never-``git add``ed file, a stale leftover.

    ``False`` -- never an exception -- in every case that is not that:
    ``path`` with no ``.git`` ancestry at all (a ``.git``-less archive
    copy, a vendored tree), a ``.git`` entry with no functioning repository
    behind it, git absent from ``$PATH``, or a git failure. Callers keep
    their filesystem-presence behaviour in all of those, so grading over a
    tree without VCS metadata is unchanged. Symlinks are resolved first:
    the file that would be hashed is the file git is asked about, and the
    containing checkout is found from that file's own directory, the way
    :func:`~klayout_tools.env_provenance.find_repo_root` walks it."""
    root = find_repo_root(os.path.dirname(os.path.realpath(path)))
    if root is None:
        return False
    tracked = _tracked_files_under(root)
    if tracked is None:
        return False
    relative = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return False
    return relative.replace(os.sep, "/") not in tracked


def repo_scoped_candidates(
    value: Any,
    spec: Mapping[str, Any],
    evidence_dir: str | None,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Every filesystem path a ``{path, scope: "repo"}`` input-artifact
    ``value`` could mean from this grading context -- ``[]`` when the value
    is not that shape, names no usable path, or no repo root can be found.

    The root is ``repo_root`` when given (``klt signoff --repo-root``,
    issue #2878 -- an explicit root is never second-guessed by a ``.git``
    walk), the one discovered from the evidence file's own location
    otherwise, which is what ``scope: "repo"`` means by construction.
    Extracted from ``signoff._resolve_input_artifact_value`` (issue #5857)
    so the resolution convention and the index-basis filter live beside
    each other."""
    if not (isinstance(value, Mapping) and value.get("scope") == "repo"):
        return []
    repo_relative = value.get("path")
    if not (isinstance(repo_relative, str) and repo_relative):
        return []
    root = (
        repo_root
        if repo_root is not None
        else find_repo_root(evidence_dir or spec.get("cwd") or os.getcwd())
    )
    if root is None:
        return []
    return [os.path.join(root, repo_relative)]
