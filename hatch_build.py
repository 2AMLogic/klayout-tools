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


def render_build_info(identity: dict[str, Any]) -> str:
    """The source text of the generated ``_build_info.py`` module."""
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
            handle.write(render_build_info(identity))

        build_data.setdefault("force_include", {})[generated] = relative_path

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        tmp_dir = getattr(self, "_tmp_dir", None)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._tmp_dir = None
