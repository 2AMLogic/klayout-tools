"""The tagged-build test (issue #5005): a release build of a real ``vX.Y.Z``
tag carries the bare version and the tag's deck history.

Builds a throwaway clone of THIS repository (full history, tags, and this
worktree's commits), lands a publisher-shaped bump commit on it, tags it, and
then runs the exact build sequence ``publish.yml`` runs:

    python scripts/generate_deck_history.py --output "$RUNNER_TEMP/_history.json"
    KLT_DECK_HISTORY_PATH=... uv build

then opens the built sdist and wheel and asserts:

- the metadata ``Version`` is exactly the tag's ``X.Y.Z`` (a build step that
  edits a tracked file would render ``X.Y.(Z+1).dev0+g<sha>.dirty`` instead);
- ``klayout_tools/decks/_history.json`` in BOTH artifacts has rows for the
  tag, each with ``git_commit`` equal to ``git rev-parse <tag>^{commit}``
  (the withdrawn ``--as-release`` plan recorded ``null``) and the tag's
  ``package_version``;
- ``git status --porcelain --untracked-files=no`` in the clone is empty
  after the whole build (the Champion review's clean-tree criterion).

``slow``: it really runs ``uv build`` (network use is whatever ``uv build``
already needs on this host -- a cached build backend needs none). Run it once
per change here: ``uv run pytest tests/test_release_build_identity.py
-m slow``.
"""

from __future__ import annotations

import email.message
import email.parser
import gzip
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_tagged_clone(tmp_path: Path) -> tuple[Path, str]:
    """A throwaway clone of this repo whose HEAD is a bump commit tagged
    ``vX.Y.Z`` with ``base-version == X.Y.Z`` -- the state ``main`` is in the
    moment the tag job tags a green release commit."""
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-hardlink",
            f"file://{REPO_ROOT}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.name", "release-build-test")
    _git(clone, "config", "user.email", "release-build-test@example.com")

    previous = "0.0.0"
    for tag in _git(clone, "tag", "--sort=-v:refname", "--list", "v*").splitlines():
        if tag.strip():
            previous = tag.strip()[1:]
            break

    # The bump, computed the way release.yml's bump job does.
    info = subprocess.run(
        [
            sys.executable,
            "-I",
            str(clone / "scripts" / "release_prep.py"),
            "--repo",
            str(clone),
            "prepare",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=clone,
    ).stdout
    version = json.loads(info)["version"]

    _git(clone, "add", "-A")
    _git(
        clone, "commit", "-q", "-m", f"chore: bump version to {version}",
    )
    tag = f"v{version}"
    _git(clone, "tag", tag)
    if previous != "0.0.0":
        assert tuple(map(int, version.split("."))) > tuple(
            map(int, previous.split("."))
        )
    return clone, tag


def _wheel_metadata_version(dist_dir: Path) -> str:
    (wheel,) = dist_dir.glob("*.whl")
    with zipfile.ZipFile(wheel) as z:
        name, = (
            n
            for n in z.namelist()
            if n.endswith(".dist-info/METADATA")
        )
        msg: email.message.Message = email.parser.Parser().parsestr(
            z.read(name).decode("utf-8")
        )
    return str(msg["Version"])


def _sdist_metadata_version(dist_dir: Path) -> str:
    (sdist,) = dist_dir.glob("*.tar.gz")
    with tarfile.open(sdist, "r:gz") as tar:
        name, = (
            n
            for n in tar.getnames()
            if n.endswith("/PKG-INFO")
        )
        chunk = tar.extractfile(name)
        assert chunk is not None
        msg: email.message.Message = email.parser.Parser().parsestr(
            chunk.read().decode("utf-8")
        )
    return str(msg["Version"])


def _wheel_history(dist_dir: Path) -> dict:
    (wheel,) = dist_dir.glob("*.whl")
    with zipfile.ZipFile(wheel) as z:
        return json.loads(z.read("klayout_tools/decks/_history.json"))


def _sdist_history(dist_dir: Path) -> dict:
    (sdist,) = dist_dir.glob("*.tar.gz")
    with tarfile.open(sdist, "r:gz") as tar:
        name, = (
            n
            for n in tar.getnames()
            if n.endswith("/src/klayout_tools/decks/_history.json")
        )
        chunk = tar.extractfile(name)
        assert chunk is not None
        return json.loads(chunk.read())


def test_tagged_build_carries_bare_version_and_tag_history(tmp_path: Path) -> None:
    clone, tag = _make_tagged_clone(tmp_path)
    version = tag[1:]
    commit = _git(clone, "rev-parse", f"{tag}^{{commit}}")

    generated = tmp_path / "runner-temp" / "_history.json"
    subprocess.run(
        [
            sys.executable,
            str(clone / "scripts" / "generate_deck_history.py"),
            "--output",
            str(generated),
        ],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )

    # The tracked file is untouched and the tree is clean BEFORE the build.
    assert _git(
        clone, "status", "--porcelain", "--untracked-files=no"
    ) == ""

    dist = clone / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
        env={"KLT_DECK_HISTORY_PATH": str(generated), "PATH": __import__("os").environ["PATH"]}
        | {k: v for k, v in __import__("os").environ.items() if k.startswith(("UV_", "HOME", "XDG_"))},
    )

    # Clean tree after the build: nothing in the sequence dirtied a tracked
    # file (a dirty tree would also have corrupted the version below).
    assert _git(
        clone, "status", "--porcelain", "--untracked-files=no"
    ) == ""

    assert _wheel_metadata_version(dist) == version
    assert _sdist_metadata_version(dist) == version

    for history in (_wheel_history(dist), _sdist_history(dist)):
        rows = [e for e in history["entries"] if e["git_tag"] == tag]
        assert rows, f"no {tag} row in the packaged deck history"
        for row in rows:
            assert row["git_commit"] == commit
            assert row["package_version"] == version


def test_tagged_build_without_the_env_var_uses_the_tracked_table(tmp_path: Path) -> None:
    """A plain ``uv build`` (a dev build, a manual sdist) keeps packaging the
    tracked table: the swap-in is opt-in via the env var only."""
    clone, tag = _make_tagged_clone(tmp_path)
    dist = clone / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": __import__("os").environ["PATH"]}
        | {k: v for k, v in __import__("os").environ.items() if k.startswith(("UV_", "HOME", "XDG_"))},
    )
    # The tag is newer than the checked-in table knows about (the table on
    # main lags by design now), so the wheel's table has NO row for it -- the
    # point is that the build succeeds and stays clean, not that it invents
    # a row.
    assert _wheel_metadata_version(dist) == tag[1:]
    assert "git_tag" in json.dumps(_wheel_history(dist))


def test_editing_a_tracked_file_before_the_build_corrupts_the_version(
    tmp_path: Path,
) -> None:
    """The regression this whole design exists to prevent: regenerating the
    tracked ``_history.json`` before ``uv build`` renders a ``.dirty`` dev
    version, not the release."""
    clone, tag = _make_tagged_clone(tmp_path)
    subprocess.run(
        [
            sys.executable,
            str(clone / "scripts" / "generate_deck_history.py"),
        ],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )
    assert _git(clone, "status", "--porcelain", "--untracked-files=no") != ""
    dist = clone / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist), "--wheel"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": __import__("os").environ["PATH"]}
        | {k: v for k, v in __import__("os").environ.items() if k.startswith(("UV_", "HOME", "XDG_"))},
    )
    built = _wheel_metadata_version(dist)
    assert built != tag[1:]
    assert ".dirty" in built
