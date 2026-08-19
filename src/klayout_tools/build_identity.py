"""Build identity of the running ``klt``: which commit is this, and is it a
tagged release? (issue #1202)

``klayout_tools.__version__`` is the *package* version -- the static string in
``pyproject.toml``. It is deliberately unchanged by this module: a wheel built
from the ``v0.2.0`` tag and a wheel built from a commit 300 changes later both
declare ``0.2.0`` to ``pip``, because that is the version they were built from.
That is correct for packaging and useless for identity: a downstream repo that
commits ``klt`` output as evidence cannot tell, from ``klt --version`` alone,
whether the build that produced it was the release it thinks it pinned or an
arbitrary post-tag source build shipping different rule decks.

This module adds the missing half -- the *build* identity:

- :func:`build_version` -- ``0.2.0`` for a build made at the matching release
  tag with a clean tree, and a PEP 440 local-version form otherwise:
  ``0.2.0+g<sha>``, ``0.2.0+g<sha>.dirty``, or ``0.2.0+unknown`` when the
  build carries no recoverable git provenance at all. A consumer can answer
  "am I on a release?" by string inspection alone, with no subprocess and no
  network.
- :func:`version_report` -- the same answer as the machine-readable payload
  behind ``klt version --format json`` (see ``docs/json-contract.md``).

Two independent sources feed it, in priority order:

1. **Recorded at build time** -- ``hatch_build.py`` (this repo's hatchling
   build hook) captures the commit/tag/dirty state of the checkout a wheel or
   sdist was built from and writes it into the distribution as
   ``klayout_tools/_build_info.py``. This is the source that matters for the
   case the issue is about: an installed wheel has no ``.git`` directory of
   its own, so without a build-time record its origin is unrecoverable.
2. **Probed live from the checkout** -- for an editable/source install (where
   the build hook deliberately records nothing, since the working tree can
   move under the install at any time), the git state is read from the
   checkout the package files actually live in.

Everything degrades to ``None``/``"unknown"`` rather than raising or
fabricating: an unresolvable identity must never be reportable as a release.
``is_release`` is therefore a tri-state, mirroring
``provenance.deck.released`` (issue #1193) -- ``True`` (confirmed release),
``False`` (confirmed *not* a release), ``None`` (unanswerable).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from . import __version__

#: Characters of the commit SHA embedded in the local-version segment. Long
#: enough to be unambiguous in this repo's history, short enough to read.
_SHORT_SHA_LEN = 12

#: Local-version segment used when a build carries no recoverable git
#: provenance (e.g. a wheel built from an unpacked tarball with no repo).
#: Deliberately *not* an empty suffix: "I cannot tell" must never render as
#: the bare release form.
_UNKNOWN_LOCAL = "unknown"

_GIT_TIMEOUT_S = 5


def _git(directory: str, *args: str) -> str | None:
    """Stripped stdout of ``git -C <directory> <args>``, or ``None`` when git
    is missing, the command fails, or it does not finish promptly. Never
    raises -- an identity probe must not be able to break ``klt --version``.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def tag_matches_package_version(tag: str | None, package_version: str) -> bool:
    """Whether ``tag`` is *this* package version's release tag.

    This repo tags releases ``vX.Y.Z`` (see ``decks/_history.json``'s
    ``git_tag`` entries); the bare ``X.Y.Z`` form is accepted too so the check
    does not depend on a tagging-style convention that costs nothing to
    tolerate. A build sitting on some *other* tag (a doc tag, a tag for a
    different version) is not this version's release.
    """
    if not tag:
        return False
    return tag == package_version or tag == f"v{package_version}"


def _resolve(
    commit: str | None,
    tag: str | None,
    dirty: bool | None,
    package_version: str,
) -> dict[str, Any]:
    """Normalise raw git facts into the identity dict every accessor reads.

    ``is_release`` is ``True`` only when *positively confirmed*: the build sat
    exactly on this package version's release tag with a clean tree. A build
    with a known commit but no matching tag is a confirmed ``False``. A build
    with no recoverable facts at all is ``None`` -- unknown, never a release.
    """
    if commit is None and tag is None:
        is_release: bool | None = None
    elif tag_matches_package_version(tag, package_version) and dirty is False:
        is_release = True
    else:
        is_release = False
    return {
        "git_commit": commit,
        "git_tag": tag,
        "dirty": dirty,
        "is_release": is_release,
    }


def _recorded_identity(package_version: str) -> dict[str, Any] | None:
    """Identity recorded at build time by ``hatch_build.py``, or ``None`` when
    this install carries no such record (an editable/source install, or a
    distribution built before this mechanism existed)."""
    try:
        from . import _build_info  # type: ignore[attr-defined]
    except ImportError:
        return None

    commit = getattr(_build_info, "GIT_COMMIT", None)
    tag = getattr(_build_info, "GIT_TAG", None)
    dirty = getattr(_build_info, "GIT_DIRTY", None)
    if not isinstance(commit, str) or not commit:
        commit = None
    if not isinstance(tag, str) or not tag:
        tag = None
    if not isinstance(dirty, bool):
        dirty = None
    return _resolve(commit, tag, dirty, package_version)


def _checkout_identity(
    directory: str | None = None, package_version: str | None = None
) -> dict[str, Any]:
    """Identity probed live from the git checkout ``directory`` lives in.

    ``directory`` defaults to this package's own source directory, so an
    editable/source install reports the checkout it is actually importing
    from. Every field is ``None`` when that directory is not part of a git
    working tree.

    A non-editable install into a virtualenv that happens to sit *inside* an
    unrelated checkout (``.venv/`` in a repo working tree) must not inherit
    that repo's HEAD, so the probe additionally requires the directory to
    contain files git actually tracks -- true for a source checkout, false
    for anything under ``site-packages``.
    """
    directory = directory or os.path.dirname(os.path.abspath(__file__))
    package_version = package_version if package_version is not None else __version__

    unknown = _resolve(None, None, None, package_version)
    if _git(directory, "rev-parse", "--is-inside-work-tree") != "true":
        return unknown
    if _git(directory, "ls-files", "--error-unmatch", "--", directory) is None:
        return unknown

    commit = _git(directory, "rev-parse", "HEAD")
    if commit is None:
        return unknown
    tag = _git(directory, "describe", "--exact-match", "--tags", "HEAD")
    status = _git(directory, "status", "--porcelain")
    dirty = None if status is None else bool(status)
    return _resolve(commit, tag, dirty, package_version)


def identity() -> dict[str, Any]:
    """The running build's ``{git_commit, git_tag, dirty, is_release}``.

    Prefers the build-time record over a live probe: a distribution's origin
    is fixed at build time, while a checkout the files merely happen to sit in
    can drift (or belong to an unrelated project) afterwards.
    """
    recorded = _recorded_identity(__version__)
    if recorded is not None:
        return recorded
    return _checkout_identity()


def git_commit() -> str | None:
    """The commit this build was made from, or ``None`` if unrecoverable."""
    return identity()["git_commit"]


def is_release() -> bool | None:
    """Tri-state: ``True`` for a confirmed tagged-release build, ``False`` for
    a confirmed non-release build, ``None`` when unanswerable."""
    return identity()["is_release"]


def format_build_version(package_version: str, ident: dict[str, Any]) -> str:
    """Render ``package_version`` plus ``ident``'s build identity as a PEP 440
    version string.

    A confirmed release renders bare (``0.2.0``) so existing consumers parsing
    ``klt X.Y.Z`` are unaffected -- that regression guard is the whole reason
    this is a local-version *suffix* rather than a different version scheme.
    """
    if ident.get("is_release") is True:
        return package_version
    commit = ident.get("git_commit")
    if commit:
        local = f"g{commit[:_SHORT_SHA_LEN]}"
        if ident.get("dirty"):
            local = f"{local}.dirty"
        return f"{package_version}+{local}"
    return f"{package_version}+{_UNKNOWN_LOCAL}"


def build_version() -> str:
    """The running build's identity-bearing version string (see
    :func:`format_build_version`)."""
    return format_build_version(__version__, identity())


def version_report() -> dict[str, Any]:
    """The ``klt version`` JSON payload (``docs/json-contract.md``).

    Flat, like every other ``klt`` payload, and carries its own
    ``schema_version`` so the dict can be reused unchanged by a future MCP
    server (see ``docs/ARCHITECTURE.md``).
    """
    ident = identity()
    return {
        "schema_version": 1,
        "version": format_build_version(__version__, ident),
        "package_version": __version__,
        "git_commit": ident["git_commit"],
        "git_tag": ident["git_tag"],
        "dirty": ident["dirty"],
        "is_release": ident["is_release"],
    }
