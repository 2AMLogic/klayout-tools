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

This module additionally exposes :func:`klayout_version_expected` (issue
#1490): the ``klayout`` engine version this build/commit was tested
against, mirroring ``identity()``'s own two-source priority (recorded at
build time in ``_build_info.py``, else probed live from the checkout's
``uv.lock`` for an editable/source install). ``klt version --format json``
reports it alongside the actually-resolved ``klayout_version``, and
``klt drc``/``klt lvs`` compare the two to populate
``provenance.klayout_version_mismatch`` -- see
``docs/design/klayout-engine-version-pin.md``.

Finally, :func:`grading_ruleset_id` (issue #2216) answers a question none of
the above can: *which grading rules* does ``klt signoff``'s tier-verdict mode
actually ship? ``git_commit``/``git_tag`` identify the source checkout, not
the grading code compiled into it -- a registry wheel built from a release
tag, a ``pip install git+...@<tag>`` snapshot of that same tag, and an
editable checkout that has moved past it on ``main`` can all report
overlapping or identical version strings while grading a tier-verdict report
by different rules. :func:`grading_ruleset_id` is a content hash of the
shipped ``signoff.py`` module, so two installs of byte-identical grading code
report the same id regardless of how each was installed, and any edit to
that module's logic changes it. ``klt version --format json`` reports it
alongside the rest of the build identity, and it is echoed into every
tier-verdict/fleet report's own ``build`` block (``signoff.py``'s
``_BUILD_IDENTITY_FIELDS``) so a committed report names the grading rules
that produced it, not just the source checkout.
"""

from __future__ import annotations

import hashlib
import os
import re
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

#: Matches a `uv.lock` `[[package]]` entry for `klayout` -- mirrors
#: `hatch_build._UV_LOCK_KLAYOUT_RE` exactly (issue #1490); duplicated rather
#: than imported since `hatch_build.py` is a build-time-only script this
#: installed package cannot import from (see that module's own docstring).
_UV_LOCK_KLAYOUT_RE = re.compile(
    r'\[\[package\]\]\s*\nname = "klayout"\s*\nversion = "([^"]+)"'
)


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


def _klayout_version() -> str | None:
    """``klayout.__version__`` (the KLayout Python engine build actually
    resolved into this process), or ``None`` when unresolvable.

    Duplicates ``_provenance._klayout_version`` (both are a three-line
    try/import) rather than importing it, so this module carries no
    dependency edge back onto ``_provenance`` -- which itself imports
    :func:`klayout_version_expected` from here to populate
    ``provenance.klayout_version_mismatch`` (issue #1490); either direction
    alone is fine, both together would be circular.
    """
    try:
        import klayout
    except Exception:
        return None
    return getattr(klayout, "__version__", None)


def _recorded_klayout_version_expected() -> str | None:
    """``_build_info.KLAYOUT_VERSION_EXPECTED`` (the ``klayout`` version this
    checkout's ``uv.lock`` pinned at build time -- see ``hatch_build.py``), or
    ``None`` when this install carries no such record."""
    try:
        from . import _build_info  # type: ignore[attr-defined]
    except ImportError:
        return None
    value = getattr(_build_info, "KLAYOUT_VERSION_EXPECTED", None)
    return value if isinstance(value, str) and value else None


def _checkout_klayout_version_expected(directory: str | None = None) -> str | None:
    """The ``klayout`` version pinned in the checkout's own ``uv.lock``,
    probed live -- the editable/source-install counterpart of
    :func:`_recorded_klayout_version_expected`, mirroring
    :func:`_checkout_identity`'s own build-time-vs-live-probe split.

    Applies the same "must be a git working tree with files this directory
    actually tracks" guard as :func:`_checkout_identity`, so a non-editable
    install into a ``.venv/`` that happens to sit inside some unrelated
    checkout does not inherit that checkout's ``uv.lock`` either.
    """
    directory = directory or os.path.dirname(os.path.abspath(__file__))
    if _git(directory, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    if _git(directory, "ls-files", "--error-unmatch", "--", directory) is None:
        return None
    root = _git(directory, "rev-parse", "--show-toplevel")
    if root is None:
        return None
    try:
        with open(os.path.join(root, "uv.lock"), encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    match = _UV_LOCK_KLAYOUT_RE.search(content)
    return match.group(1) if match else None


def klayout_version_expected() -> str | None:
    """The ``klayout`` engine version this build/commit was tested against
    (issue #1490) -- the answer ``provenance.klayout_version_mismatch`` and
    ``klt version --format json``'s ``klayout_version_expected`` compare the
    actually-resolved ``klayout_version`` against.

    Two sources, in the same priority order as :func:`identity`: recorded at
    build time (from the checkout's ``uv.lock`` at build time, via
    ``hatch_build.py``), else probed live from an editable/source install's
    own ``uv.lock``. ``None`` when neither source has an answer -- a build
    made before this field existed, or a checkout with no reachable
    ``uv.lock`` (e.g. an unpacked sdist with no git history).
    """
    recorded = _recorded_klayout_version_expected()
    if recorded is not None:
        return recorded
    return _checkout_klayout_version_expected()


def _grading_module_path() -> str:
    """Absolute path to ``signoff.py`` next to this module, in whichever
    install layout is running -- a wheel unpacks it into ``site-packages``
    beside this file, an editable/source install has it in the same
    ``src/klayout_tools`` directory. Never imports the module itself: this
    only needs its *bytes*, not its behaviour, and ``signoff.py`` already
    imports :func:`version_report` from this module, so importing back would
    be circular.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "signoff.py")


def grading_ruleset_id() -> str | None:
    """A content hash identifying the grading rules ``klt signoff``'s
    tier-verdict mode ships in *this* build (issue #2216) -- the missing
    half ``git_commit``/``git_tag`` cannot answer.

    Three installs can report the exact same ``klt`` version string while
    grading a tier-verdict report by different rules: a registry wheel built
    from a release tag, a ``pip install git+...@<tag>`` snapshot of that
    same tag, and a full-repo checkout that has moved past the tag on
    ``main``. ``git_commit``/``git_tag`` identify the *source checkout* each
    of those was built from, not the grading logic actually compiled into
    the running build -- and a consumer who commits a tier-verdict report as
    evidence has no way to tell, from ``klt version`` alone, which grading
    rules produced it.

    Deliberately a **content** hash of the shipped ``signoff.py`` module
    rather than something derived from ``git_commit``:

    - Two installs built from the *same* commit -- a PyPI-published wheel
      and a ``git+...`` snapshot of the same release tag -- ship
      byte-identical ``signoff.py`` source and must report the *same* id,
      with no shared git history to compare against (a wheel's
      ``site-packages`` install carries no ``.git`` directory at all).
      Hashing the file achieves that for free; comparing commits does not.
    - An edit to ``signoff.py``'s grading logic changes this id even when
      unrelated files elsewhere in the repo also changed in the same commit
      -- a stable, scoped answer to "did the grading rules change", rather
      than the much coarser "did *anything* change" ``git_commit`` answers.

    Hashes the **whole** ``signoff.py`` module (not just its per-item
    grading functions): the module has no sharp internal boundary between
    "grading logic" and "everything else" that would not itself need
    updating -- and require re-verification -- every time a helper is
    renamed or refactored across that line. A doc-only edit inside
    ``signoff.py`` (e.g. a docstring correction) does move this id; that
    false-positive is the deliberately conservative trade-off for never
    missing a real grading-logic change, mirroring
    :func:`~.design_evidence_tiers.parse_tier_doc`'s own whole-file
    ``content_hash`` for the checklist doc (issue #2175) rather than a
    narrower, harder-to-keep-accurate per-item extraction.

    ``None`` only in the pathological case where the shipped module cannot
    be read at all (e.g. a corrupted or hand-truncated install) -- never
    fabricated.
    """
    path = _grading_module_path()
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


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

    ``grading_ruleset_id`` (issue #2216) is purely additive like
    ``klayout_version``/``klayout_version_expected`` before it (issue #1490)
    -- a new field within an unchanged shape earns no ``schema_version``
    bump under ``docs/json-contract.md``'s "adding new fields does not
    require a bump" rule, the same policy ``signoff.py``'s
    ``TIER_REPORT_SCHEMA_VERSION`` documents staying unbumped across its own
    purely-additive ``build``/``graded_by_build``/``build_t1_item_count``
    fields (issues #2176, #2202).
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
        # Issue #2216: a content hash of the grading rules `klt signoff`'s
        # tier-verdict mode ships -- distinct from git_commit/git_tag, which
        # identify the checkout, not the grading code. See
        # `grading_ruleset_id`'s own docstring for why a content hash and
        # not a derivation of git_commit.
        "grading_ruleset_id": grading_ruleset_id(),
        # Issue #1490: the KLayout engine this process actually resolved,
        # and the version this build/commit was tested against -- a caller
        # can detect a drifted engine before trusting a "reproduced" report
        # without waiting for a `klt drc`/`klt lvs` run to say so.
        "klayout_version": _klayout_version(),
        "klayout_version_expected": klayout_version_expected(),
    }
