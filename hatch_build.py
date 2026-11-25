"""Hatchling build plugins: compute the distribution version (issue #2823)
and record the git identity of the checkout a distribution was built from
(issue #1202).

**Version.** ``pyproject.toml``'s ``[tool.hatch.version]`` table points
hatchling's built-in ``code`` version source at this file and evaluates
:func:`version_from_pyproject`, which applies
``src/klayout_tools/_version_scheme.py`` (loaded by path -- the same module
the runtime uses) to the table's ``base-version`` and the checkout's git
facts. Only a clean build exactly on the release tag declares the bare
``X.Y.Z``; every other build declares a distinct public version
(``X.Y.(Z+1).devN+g<sha>`` and friends), so a resolver can never satisfy
``klayout-tools==X.Y.Z`` with it. A build from an unpacked sdist (no
``.git``) keeps the version the sdist was built with: hatchling reuses
``PKG-INFO``'s version, and :func:`distribution_version` falls back to the
``VERSION`` the sdist's carried ``_build_info.py`` records.

**Identity.** An installed wheel has no ``.git`` directory, so once it is
built, the commit it came from is unrecoverable at runtime. This hook writes
a tiny generated module, ``klayout_tools/_build_info.py``, into the
distribution:

    GIT_COMMIT = "<40-hex sha>"   # or None
    GIT_TAG = "v0.2.0"            # exact-match tag, or None
    GIT_DIRTY = False             # uncommitted changes to *tracked* files at build time
    KLAYOUT_VERSION_EXPECTED = "0.30.10"  # or None
    DECK_HASHES = {"sky130": "sha256:<hex>", ...}  # or None
    VERSION = "0.2.1.dev3+g<sha>"  # the distribution version chosen above

``DECK_HASHES`` (issue #2451) records the content hash of every built-in deck
and its declared rule sources, keyed by primary module stem, *as built*.
:mod:`klayout_tools.decks.history` lets a release build vouch for its own
decks only when a queried hash equals the hash recorded here -- never the
current on-disk bytes, which a post-install edit could change while the
frozen ``GIT_TAG``/``GIT_DIRTY`` record still says "clean release".

``KLAYOUT_VERSION_EXPECTED`` (issue #1490) records the ``klayout`` engine
version this checkout's ``uv.lock`` pins at build time -- the version CI
actually ran the test suite against for this commit (CI installs via ``uv
sync --locked``, so a locked ``uv.lock`` entry *is* "the version this
release/commit was tested against"; ``pyproject.toml``'s own
``klayout>=0.30`` dependency is deliberately just a floor, see that file's
comment). :mod:`klayout_tools.build_identity` compares it against the
``klayout`` actually resolved at runtime to populate
``provenance.klayout_version_mismatch`` on ``klt drc``/``klt lvs`` reports
and ``klayout_version``/``klayout_version_expected`` on ``klt version
--format json``. See ``docs/design/klayout-engine-version-pin.md`` for the
full rationale.

:mod:`klayout_tools.build_identity` reads the git facts and derives
``is_release`` from them. The *policy* (what counts as a release, which
version a build declares) deliberately lives in the package --
``build_identity`` and ``_version_scheme`` -- not here, so it is unit-tested
as ordinary package code and the build and runtime share one implementation.

Design notes:

- **Never written into the source tree.** The generated module is materialised
  in a temp directory and force-included into the distribution, so a build
  never leaves an untracked file behind in a working checkout.
- **Editable installs are skipped on purpose.** An editable install imports
  from the live checkout, whose git state changes with every commit; a
  build-time snapshot would go stale immediately. ``build_identity`` probes
  the checkout live in that case instead.
- **The sdist gets one too.** ``uv build`` builds the wheel from the sdist,
  where no ``.git`` exists -- so the sdist must carry the record forward or a
  real PyPI release wheel would report an unknown identity. When git facts
  are unavailable the hook writes nothing at all rather than overwriting a
  record the sdist already carries.
- **Never fails the build.** Any git problem degrades to "no record" and a
  ``X.Y.Z.dev0+unknown`` version -- honest, and never mistakable for a
  release.
- **The release build swaps in a tag-generated deck history (issue #5005).**
  When ``KLT_DECK_HISTORY_PATH`` names a file outside the build root (the
  release build writes ``$RUNNER_TEMP``), it is force-included as the
  packaged ``decks/_history.json`` in wheel and sdist, replacing the tracked
  copy in the artifact only: editing the tracked file would dirty the
  checkout and turn the release version into ``X.Y.(Z+1).dev…+dirty``.
- **Untracked files never count as dirty (issue #2248).** ``git_identity()``'s
  ``dirty`` probe ignores untracked files on purpose -- a package manager
  building a ``git+...@<sha>`` install clones into a scratch checkout that it
  can itself litter with bookkeeping files (observed: ``uv`` drops its own
  ``.ok`` checkout-completion sentinel into the tree before the build even
  starts), which previously made a clean, immutable, sha-pinned revision
  render as ``dirty: true``. See that function's docstring for the full
  mechanism.
"""

from __future__ import annotations

import os
import re
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:  # pragma: no cover - hatchling is present only in the build environment
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # pragma: no cover - lets the helpers below be unit-tested
    BuildHookInterface = object  # type: ignore[assignment,misc]

#: Path of the generated module inside each distribution target.
_RELATIVE_PATHS = {
    "wheel": "klayout_tools/_build_info.py",
    "sdist": "src/klayout_tools/_build_info.py",
}

#: Path of the packaged deck-history table inside each distribution target
#: (issue #5005): same layout as the tracked file, but the CONTENT comes from
#: ``KLT_DECK_HISTORY_PATH`` when the release build sets it.
_HISTORY_RELATIVE_PATHS = {
    "wheel": "klayout_tools/decks/_history.json",
    "sdist": "src/klayout_tools/decks/_history.json",
}

#: Environment variable ``publish.yml``'s build job sets to swap in the
#: tag-generated deck-history table without touching a tracked file (which
#: would dirty the checkout and corrupt the release version, issue #5005).
_HISTORY_PATH_ENV = "KLT_DECK_HISTORY_PATH"

_GIT_TIMEOUT_S = 15

#: Matches a ``uv.lock`` ``[[package]]`` entry for ``klayout`` -- ``uv.lock``
#: always emits ``name = "..."`` immediately followed by ``version = "..."``
#: (verified against this repo's own lockfile), so a small anchored regex is
#: enough without adding a TOML-parsing dependency (``tomllib`` is stdlib
#: only from Python 3.11, and this build hook must work under the 3.10 floor
#: `pyproject.toml` declares).
_UV_LOCK_KLAYOUT_RE = re.compile(
    r'\[\[package\]\]\s*\nname = "klayout"\s*\nversion = "([^"]+)"'
)


def klayout_version_expected(root: str) -> str | None:
    """The ``klayout`` version pinned in ``<root>/uv.lock``, or ``None`` when
    the lockfile is missing, unreadable, or carries no ``klayout`` entry
    (issue #1490).

    ``uv.lock`` is committed to the repo and consumed by CI via ``uv sync
    --locked``, so its pinned ``klayout`` entry is the version this exact
    commit's test suite actually ran against -- the answer to "what should a
    caller reproducing this build's reports install" that ``pyproject.toml``'s
    unbounded ``klayout>=0.30`` floor cannot give on its own.
    """
    try:
        with open(os.path.join(root, "uv.lock"), encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    match = _UV_LOCK_KLAYOUT_RE.search(content)
    return match.group(1) if match else None


#: Source directory of the built-in deck modules, relative to the build root.
_DECKS_DIR = os.path.join("src", "klayout_tools", "decks")


def deck_hashes(root: str) -> dict[str, str] | None:
    """``{module_stem: "sha256:<hex>"}`` for every ``*.py`` module in
    ``<root>/src/klayout_tools/decks``, or ``None`` when that directory is
    missing/unreadable (issue #2451). A standalone KLayout DRC deck file is
    recorded under its full filename (``"asap7.drc"``) -- a separate artifact
    from the ``asap7`` module (issue #3826).

    Hashes every module rather than importing the package to ask which are
    decks: the build hook must not import the package it is building, and a
    surplus entry (``rules.py``) is harmless because the runtime only ever
    looks up real deck names.
    """
    directory = os.path.join(root, _DECKS_DIR)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    # Load the dependency-free digest by path in the isolated Hatch environment.
    source_file_hash = runpy.run_path(
        str(Path(__file__).parent / "src/klayout_tools/_deck_hash.py")
    )["source_file_hash"]
    hashes: dict[str, str] = {}
    for filename in names:
        if filename.endswith((".py", ".drc")):
            digest = source_file_hash(os.path.join(directory, filename))
            if digest is not None:
                hashes[filename.removesuffix(".py")] = digest
    return hashes or None


def _git(root: str, *args: str) -> str | None:
    """Stripped stdout of ``git -C <root> <args>``, or ``None`` on any
    failure (git missing, not a repo, non-zero exit, timeout)."""
    try:
        completed = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_identity(root: str) -> dict[str, Any] | None:
    """``{commit, tag, dirty}`` for the checkout at ``root``, or ``None`` when
    ``root`` is not a git working tree (a build from an unpacked sdist).

    ``dirty`` is computed from ``git status --porcelain --untracked-files=no``
    -- deliberately excluding *untracked* files (issue #2248). A build root
    is not necessarily the user's own working copy: ``uv``/``pip`` building a
    ``git+https://...@<sha>`` install clone the repo into a scratch checkout
    and can themselves drop bookkeeping files into it before invoking this
    hook (reproduced: ``uv`` writes its own ``.ok`` checkout-completion
    sentinel straight into the git working tree immediately after cloning,
    strictly before the build backend -- and this hook -- ever runs, so no
    reordering of this probe can outrun it). Such stray untracked files carry
    no packaged content and say nothing about whether the *pinned revision*
    was modified, so counting them as "dirty" mislabels an immutable,
    sha-pinned install as locally modified. Restricting the probe to tracked
    content (modified/added/deleted/renamed files already known to git)
    keeps ``dirty`` meaningful -- a real edit to an existing tracked file
    still trips it -- while no longer reacting to build-tool noise outside
    git's own bookkeeping. This mirrors the convention ``git describe
    --dirty`` and ``setuptools_scm`` already use.
    """
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    commit = _git(root, "rev-parse", "HEAD")
    if not commit:
        return None
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit,
        "tag": _git(root, "describe", "--exact-match", "--tags", "HEAD") or None,
        "dirty": bool(status) if status is not None else None,
    }


def render_build_info(
    identity: dict[str, Any],
    klayout_version_expected: str | None = None,
    deck_hashes: dict[str, str] | None = None,
    version: str | None = None,
) -> str:
    """The source text of the generated ``_build_info.py`` module.

    ``klayout_version_expected`` (issue #1490), ``deck_hashes`` (issue
    #2451) and ``version`` (issue #2823 -- the distribution version this
    build declared, so a wheel rebuilt from the sdist can carry it forward)
    default to ``None`` so existing callers passing only ``identity`` keep
    working unchanged.
    """
    return (
        '"""Generated at build time by ``hatch_build.py`` -- do not edit.\n'
        "\n"
        "Records the git identity of the checkout this distribution was built\n"
        "from; read by :mod:`klayout_tools.build_identity` (issue #1202).\n"
        '"""\n'
        "\n"
        f"GIT_COMMIT = {identity['commit']!r}\n"
        f"GIT_TAG = {identity['tag']!r}\n"
        f"GIT_DIRTY = {identity['dirty']!r}\n"
        f"KLAYOUT_VERSION_EXPECTED = {klayout_version_expected!r}\n"
        f"DECK_HASHES = {deck_hashes!r}\n"
        f"VERSION = {version!r}\n"
    )


#: The shared version-scheme module, relative to the build root. Loaded by
#: path so the build never imports the package it is building.
_VERSION_SCHEME_PATH = os.path.join("src", "klayout_tools", "_version_scheme.py")

#: Matches the ``VERSION = "..."`` line of a previously generated
#: ``_build_info.py`` (the record an sdist carries).
_RECORDED_VERSION_RE = re.compile(r'(?m)^VERSION = [\'"]([^\'"]+)[\'"]')


def load_version_scheme(root: str) -> Any:
    """``src/klayout_tools/_version_scheme.py`` under ``root``, loaded by
    path -- the single implementation of the version scheme shared with the
    runtime (issue #2823)."""
    import importlib.util

    path = os.path.join(root, _VERSION_SCHEME_PATH)
    spec = importlib.util.spec_from_file_location("_klt_version_scheme", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load the version scheme from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recorded_version(root: str) -> str | None:
    """The ``VERSION`` an sdist's carried ``_build_info.py`` record declares,
    or ``None`` when there is no such record (or it predates the field)."""
    path = os.path.join(root, _RELATIVE_PATHS["sdist"])
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    match = _RECORDED_VERSION_RE.search(content)
    return match.group(1) if match else None


def distribution_version(root: str, base: str) -> str:
    """The PEP 440 version a build of ``root`` declares (issue #2823).

    From a git checkout: the shared scheme applied to its git facts. From an
    unpacked sdist (no ``.git``): the version the sdist's own record carries
    forward, so the sdist-to-wheel path preserves the originating checkout's
    version (hatchling also reuses ``PKG-INFO``'s version in that case; this
    is the belt to that brace). Neither: ``<base>.dev0+unknown`` -- never the
    bare release.
    """
    scheme = load_version_scheme(root)
    facts = scheme.probe(root, base)
    if facts is None:
        carried = recorded_version(root)
        if carried:
            return carried
        return scheme.render(base, None, None, None, None, None)
    return scheme.render(base, **facts)


def version_from_pyproject(root: str | None = None) -> str:
    """The distribution version for the checkout (or unpacked sdist) at
    ``root`` -- this file's directory by default -- using ``pyproject.toml``'s
    ``[tool.hatch.version]`` ``base-version`` (issue #2823).

    This is the expression hatchling's built-in ``code`` version source
    evaluates (``pyproject.toml``'s ``[tool.hatch.version]`` table): hatchling
    offers no ``custom`` version-source plugin, so the ``code`` source loads
    this file by path and calls this function.
    """
    root = root or os.path.dirname(os.path.abspath(__file__))
    scheme = load_version_scheme(root)
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as handle:
        base = scheme.read_base_version(handle.read())
    if not base:
        raise ValueError(
            '[tool.hatch.version] in pyproject.toml must set `base-version = "X.Y.Z"`'
        )
    return distribution_version(root, base)


class BuildIdentityHook(BuildHookInterface):  # type: ignore[misc,valid-type]
    """Force-includes the generated ``_build_info.py`` into wheel and sdist."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        self._tmp_dir: str | None = None

        relative_path = _RELATIVE_PATHS.get(self.target_name)
        history_relative = _HISTORY_RELATIVE_PATHS.get(self.target_name)
        if version == "editable":
            return

        # The release build's deck history (issue #5005): publish.yml checks
        # out the tag, generates the table from that real tag into
        # $RUNNER_TEMP and points this hook at it. The forced source lives
        # OUTSIDE the build root, which is what makes hatchling treat the
        # target path as reserved and skip the tracked file there -- the
        # generated table replaces it in the artifact while the checkout (and
        # so the version this build declares) stays clean. Applied before the
        # git-identity early return so the wheel built from the sdist (no
        # .git, identity None) keeps getting it too.
        history_source = os.environ.get(_HISTORY_PATH_ENV)
        if history_relative and history_source:
            if not os.path.isfile(history_source):
                raise RuntimeError(
                    f"{_HISTORY_PATH_ENV}={history_source!r} does not exist;"
                    " unset it or generate the table first"
                )
            build_data.setdefault("force_include", {})[
                history_source
            ] = history_relative

        if relative_path is None:
            return

        identity = git_identity(self.root)
        if identity is None:
            # No git facts to record. Leave whatever the source tree already
            # carries (an sdist-provided record) untouched.
            return

        self._tmp_dir = tempfile.mkdtemp(prefix="klt-build-info-")
        generated = os.path.join(self._tmp_dir, "_build_info.py")
        with open(generated, "w", encoding="utf-8") as handle:
            handle.write(
                render_build_info(
                    identity,
                    klayout_version_expected(self.root),
                    deck_hashes(self.root),
                    self.metadata.version,
                )
            )

        build_data.setdefault("force_include", {})[generated] = relative_path

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        tmp_dir = getattr(self, "_tmp_dir", None)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._tmp_dir = None
