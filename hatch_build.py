"""Hatchling build hook: record the git identity of the checkout a
distribution was built from (issue #1202).

An installed wheel has no ``.git`` directory, so once it is built, the commit
it came from is unrecoverable at runtime. That is exactly what made
``klt --version`` unable to distinguish a PyPI release from a
``pip install git+...@<sha>`` build of the same version number: both declare
the static ``version`` from ``pyproject.toml``, and nothing else on the CLI
surface separates them.

This hook closes that by writing a tiny generated module,
``klayout_tools/_build_info.py``, into the distribution:

    GIT_COMMIT = "<40-hex sha>"   # or None
    GIT_TAG = "v0.2.0"            # exact-match tag, or None
    GIT_DIRTY = False             # uncommitted changes at build time
    KLAYOUT_VERSION_EXPECTED = "0.30.10"  # or None

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

:mod:`klayout_tools.build_identity` reads those three facts and derives the
reported version/``is_release`` from them -- the *policy* (what counts as a
release) deliberately lives there, in the package, not here in the build
hook, so it can be tested and changed without rebuilding anything.

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
- **Never fails the build.** Any git problem degrades to "no record", which
  ``build_identity`` reports as ``+unknown`` -- honest, and never mistakable
  for a release.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
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
    ``root`` is not a git working tree (a build from an unpacked sdist)."""
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    commit = _git(root, "rev-parse", "HEAD")
    if not commit:
        return None
    status = _git(root, "status", "--porcelain")
    return {
        "commit": commit,
        "tag": _git(root, "describe", "--exact-match", "--tags", "HEAD") or None,
        "dirty": bool(status) if status is not None else None,
    }


def render_build_info(
    identity: dict[str, Any], klayout_version_expected: str | None = None
) -> str:
    """The source text of the generated ``_build_info.py`` module.

    ``klayout_version_expected`` (issue #1490) defaults to ``None`` so
    existing callers passing only ``identity`` keep working unchanged.
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
    )


class BuildIdentityHook(BuildHookInterface):  # type: ignore[misc,valid-type]
    """Force-includes the generated ``_build_info.py`` into wheel and sdist."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        self._tmp_dir: str | None = None

        relative_path = _RELATIVE_PATHS.get(self.target_name)
        if relative_path is None or version == "editable":
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
                render_build_info(identity, klayout_version_expected(self.root))
            )

        build_data.setdefault("force_include", {})[generated] = relative_path

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        tmp_dir = getattr(self, "_tmp_dir", None)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._tmp_dir = None
