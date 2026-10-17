"""The distribution version scheme: which PEP 440 version a build of this
checkout declares (issue #2823).

``pyproject.toml`` declares a *base* version (``[tool.hatch.version]``'s
``base-version``) -- the release the checkout is at or working towards. The
version a built wheel/sdist actually carries in its metadata is derived from
that base plus the checkout's git facts:

======================================================  ==========================
Build                                                   Distribution version
======================================================  ==========================
Clean tree sitting exactly on the base's release tag    ``X.Y.Z``
Any commit after the released base's tag                ``X.Y.(Z+1).devN+g<sha>``
A commit before the base has been tagged                ``X.Y.Z.devN+g<sha>``
Either of the above with uncommitted tracked changes    ``...+g<sha>.dirty``
No recoverable git facts at all                         ``X.Y.Z.dev0+unknown``
======================================================  ==========================

``N`` is the number of commits since the base's release tag when that tag is
an ancestor of ``HEAD``; otherwise the number of commits since the nearest
tag reachable from ``HEAD``; otherwise the number of commits reachable from
``HEAD``.

The property this exists for: **only a clean build at the release tag
carries the bare public version** ``X.Y.Z``. Every other build's *public*
version differs (a ``.devN`` release segment), so a PEP 440 ``==X.Y.Z``
specifier -- which ignores the ``+local`` segment but not ``.devN`` --
cannot be satisfied by a post-tag, dirty, or unknown build. The previous
scheme put the commit only in the local segment of a ``klt``-printed string
while the wheel metadata stayed the bare base, so a cached dev build
silently satisfied ``klayout-tools==X.Y.Z`` (issue #2823).

Ordering: a post-release dev build sorts after ``X.Y.Z`` and before the next
patch release; a dev build of a not-yet-tagged base sorts before that base.
An unknown build sorts before its base because it cannot prove it is at or
past it.

Deliberately stdlib-only with no package-relative imports: the build hook
(``hatch_build.py``) loads this file by path at build time, without
importing the package it is building, and :mod:`klayout_tools.build_identity`
imports it at runtime for editable installs -- one implementation for both,
so the build and the runtime can never derive different versions from the
same facts.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

#: Characters of the commit SHA embedded in the local-version segment. Long
#: enough to be unambiguous in this repo's history, short enough to read.
SHORT_SHA_LEN = 12

#: Local-version segment of a build with no recoverable git provenance.
UNKNOWN_LOCAL = "unknown"

_GIT_TIMEOUT_S = 15

_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: ``[tool.hatch.version]``'s ``base-version = "X.Y.Z"`` line in
#: ``pyproject.toml``. A regex rather than a TOML parser because ``tomllib``
#: is stdlib only from Python 3.11 and the floor is 3.10.
BASE_VERSION_RE = re.compile(r'(?m)^base-version\s*=\s*"([^"]+)"')


def _git(root: str, *args: str, timeout: float = _GIT_TIMEOUT_S) -> str | None:
    """Stripped stdout of ``git -C <root> <args>``, or ``None`` on any
    failure (git missing, not a repo, non-zero exit, timeout)."""
    try:
        completed = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def release_tags(base: str) -> tuple[str, str]:
    """The tag spellings accepted as ``base``'s release tag: ``vX.Y.Z``
    (this repo's convention) and bare ``X.Y.Z``."""
    return (f"v{base}", base)


def tag_is_release_of(tag: str | None, base: str) -> bool:
    """Whether ``tag`` is ``base``'s release tag."""
    return bool(tag) and tag in release_tags(base)


def next_release(base: str) -> str:
    """The version a post-release dev build is working towards: the next
    patch release for a plain ``X.Y.Z``, else ``<base>.post0`` (still a
    valid PEP 440 release segment that sorts after ``base``)."""
    match = _RELEASE_RE.match(base)
    if match is None:
        return f"{base}.post0"
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def render(
    base: str,
    commit: str | None,
    tag: str | None,
    dirty: bool | None,
    released: bool | None,
    distance: int | None,
) -> str:
    """The PEP 440 distribution version for ``base`` and these git facts.

    - ``commit``: full SHA of ``HEAD``, or ``None`` when unrecoverable.
    - ``tag``: the tag ``HEAD`` sits exactly on, or ``None``.
    - ``dirty``: uncommitted changes to tracked files (``None`` = unknown).
    - ``released``: whether ``base``'s release tag is ``HEAD`` or one of its
      ancestors (``None``/``False`` = not known to be released).
    - ``distance``: commits counted per this module's docstring.

    The bare ``base`` is returned **only** for a known commit, exactly on
    ``base``'s release tag, with a tree positively known to be clean.
    """
    if not commit:
        return f"{base}.dev0+{UNKNOWN_LOCAL}"
    if tag_is_release_of(tag, base) and dirty is False:
        return base
    target = next_release(base) if released else base
    local = f"g{commit[:SHORT_SHA_LEN]}"
    if dirty is True:
        local = f"{local}.dirty"
    return f"{target}.dev{max(int(distance or 0), 0)}+{local}"


def _count(root: str, revision_range: str) -> int | None:
    out = _git(root, "rev-list", "--count", revision_range)
    try:
        return int(out) if out is not None else None
    except ValueError:
        return None


def probe(root: str, base: str) -> dict[str, Any] | None:
    """The git facts :func:`render` needs, for the checkout at ``root``, or
    ``None`` when ``root`` is not a git working tree (e.g. an unpacked sdist).

    ``dirty`` ignores untracked files (issue #2248; see
    ``hatch_build.git_identity``'s docstring for why).
    """
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    commit = _git(root, "rev-parse", "HEAD")
    if not commit:
        return None
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    tag = _git(root, "describe", "--exact-match", "--tags", "HEAD") or None

    released = False
    distance: int | None = None
    for candidate in release_tags(base):
        if (
            _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{candidate}")
            is None
        ):
            continue
        if _git(root, "merge-base", "--is-ancestor", candidate, "HEAD") is not None:
            released = True
            distance = _count(root, f"{candidate}..HEAD")
            break
    if distance is None:
        nearest = _git(root, "describe", "--tags", "--abbrev=0", "HEAD")
        distance = _count(root, f"{nearest}..HEAD") if nearest else _count(root, "HEAD")

    return {
        "commit": commit,
        "tag": tag,
        "dirty": bool(status) if status is not None else None,
        "released": released,
        "distance": distance,
    }


def version_for_checkout(root: str, base: str) -> str:
    """:func:`render` applied to :func:`probe` -- the version a build of the
    checkout at ``root`` declares right now."""
    facts = probe(root, base)
    if facts is None:
        return render(base, None, None, None, None, None)
    return render(base, **facts)


def read_base_version(pyproject_text: str) -> str | None:
    """``base-version`` from ``pyproject.toml``'s text, or ``None``."""
    match = BASE_VERSION_RE.search(pyproject_text)
    return match.group(1) if match else None
