"""The distribution version scheme (issue #2823).

Every version the scheme can emit is validated with a real PEP 440
implementation (``packaging``), and the property the issue is about -- only a
clean build at the release tag satisfies ``==X.Y.Z`` -- is checked with a
standards-based specifier, not by string comparison.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from klayout_tools import _version_scheme as scheme

packaging_version = pytest.importorskip("packaging.version")
packaging_specifiers = pytest.importorskip("packaging.specifiers")

Version = packaging_version.Version
SpecifierSet = packaging_specifiers.SpecifierSet

_SHA = "abcdef0123456789" + "0" * 24


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, content: str, message: str) -> None:
    (repo / "tracked.py").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _make_repo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, "init", "-q", ".")
    _commit(directory, "x = 1\n", "initial")
    return directory


def _pins(version: str, base: str) -> bool:
    """Whether a resolver would accept ``version`` for ``==base``."""
    return SpecifierSet(f"=={base}").contains(Version(version), prereleases=True)


# --------------------------------------------------------------------------- #
# render(): pure policy
# --------------------------------------------------------------------------- #


def test_clean_build_at_the_release_tag_is_the_bare_release():
    """Regression guard: a real release keeps reporting bare ``X.Y.Z``."""
    version = scheme.render("1.2.3", _SHA, "v1.2.3", False, True, 0)
    assert version == "1.2.3"
    assert _pins(version, "1.2.3")


def test_unprefixed_release_tag_is_accepted():
    assert scheme.render("1.2.3", _SHA, "1.2.3", False, True, 0) == "1.2.3"


@pytest.mark.parametrize(
    "args,expected",
    [
        # Post-tag: next patch, commit distance, commit in the local segment.
        (("1.2.3", _SHA, None, False, True, 7), "1.2.4.dev7+gabcdef012345"),
        # Dirty tree exactly on the release tag: not the release.
        (("1.2.3", _SHA, "v1.2.3", True, True, 0), "1.2.4.dev0+gabcdef012345.dirty"),
        # Dirty state unknown on the release tag: not the release either.
        (("1.2.3", _SHA, "v1.2.3", None, True, 0), "1.2.4.dev0+gabcdef012345"),
        # Base not tagged yet (version bumped before tagging, or tag not
        # visible to the build): a dev release *of* the base.
        (("1.3.0", _SHA, None, False, False, 4), "1.3.0.dev4+gabcdef012345"),
        # A tag for some other version is not this base's release.
        (("1.2.3", _SHA, "v0.9.0", False, False, 2), "1.2.3.dev2+gabcdef012345"),
        # No git facts at all.
        (("1.2.3", None, None, None, None, None), "1.2.3.dev0+unknown"),
    ],
    ids=[
        "post-tag",
        "dirty-at-tag",
        "unknown-dirty",
        "untagged-base",
        "other-tag",
        "unknown",
    ],
)
def test_non_release_builds_never_satisfy_the_exact_pin(args, expected):
    version = scheme.render(*args)
    assert version == expected
    assert str(Version(version)) == version  # valid, already-normalised PEP 440
    assert not _pins(version, args[0])


def test_post_tag_builds_order_between_the_release_and_the_next_patch():
    release = Version(scheme.render("1.2.3", _SHA, "v1.2.3", False, True, 0))
    first = Version(scheme.render("1.2.3", _SHA, None, False, True, 1))
    later = Version(scheme.render("1.2.3", _SHA, None, False, True, 9))
    assert release < first < later < Version("1.2.4")


def test_untagged_base_and_unknown_order_before_the_base():
    assert Version(scheme.render("1.3.0", _SHA, None, False, False, 4)) < Version(
        "1.3.0"
    )
    assert Version(scheme.render("1.3.0", None, None, None, None, None)) < Version(
        "1.3.0"
    )


def test_next_release_handles_non_plain_bases():
    assert scheme.next_release("1.2.3") == "1.2.4"
    rendered = scheme.render("1.3.0rc1", _SHA, None, False, True, 2)
    assert Version(rendered) > Version("1.3.0rc1")
    assert not _pins(rendered, "1.3.0rc1")


def test_read_base_version():
    text = '[tool.hatch.version]\nsource = "code"\nbase-version = "4.5.6"\n'
    assert scheme.read_base_version(text) == "4.5.6"
    assert scheme.read_base_version("[project]\nname = 'x'\n") is None


def test_repo_pyproject_declares_a_plain_base_version():
    root = Path(__file__).resolve().parents[1]
    base = scheme.read_base_version((root / "pyproject.toml").read_text("utf-8"))
    assert base is not None
    assert str(Version(base)) == base
    assert not Version(base).is_prerelease and Version(base).local is None


# --------------------------------------------------------------------------- #
# probe(): real git checkouts
# --------------------------------------------------------------------------- #


def test_probe_none_outside_a_repo(tmp_path):
    assert scheme.probe(str(tmp_path), "1.2.3") is None
    assert scheme.version_for_checkout(str(tmp_path), "1.2.3") == "1.2.3.dev0+unknown"


def test_checkout_at_the_release_tag(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "tag", "v1.2.3")
    assert scheme.version_for_checkout(str(repo), "1.2.3") == "1.2.3"


def test_checkout_after_the_release_tag_counts_commits_since_it(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "tag", "v1.2.3")
    _commit(repo, "x = 2\n", "one")
    _commit(repo, "x = 3\n", "two")
    sha = _git(repo, "rev-parse", "HEAD")
    version = scheme.version_for_checkout(str(repo), "1.2.3")
    assert version == f"1.2.4.dev2+g{sha[:12]}"
    assert not _pins(version, "1.2.3")


def test_dirty_checkout_at_the_release_tag(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "tag", "v1.2.3")
    (repo / "tracked.py").write_text("x = 99\n")
    sha = _git(repo, "rev-parse", "HEAD")
    assert scheme.version_for_checkout(str(repo), "1.2.3") == (
        f"1.2.4.dev0+g{sha[:12]}.dirty"
    )


def test_untracked_files_do_not_make_a_checkout_dirty(tmp_path):
    """Issue #2248: package-manager residue must not cost a clean build its
    release version."""
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "tag", "v1.2.3")
    (repo / ".ok").write_text("")
    assert scheme.version_for_checkout(str(repo), "1.2.3") == "1.2.3"


def test_base_bumped_before_tagging_counts_from_the_previous_tag(tmp_path):
    """The release flow bumps ``base-version`` on main before tagging, so the
    bump commit is a dev release *of* the new base, not of the one after."""
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "tag", "v1.2.3")
    _commit(repo, "x = 2\n", "bump to 1.3.0")
    sha = _git(repo, "rev-parse", "HEAD")
    assert scheme.version_for_checkout(str(repo), "1.3.0") == f"1.3.0.dev1+g{sha[:12]}"
    # ...and the moment it is tagged, it is the release.
    _git(repo, "tag", "v1.3.0")
    assert scheme.version_for_checkout(str(repo), "1.3.0") == "1.3.0"


def test_no_tags_at_all_counts_every_commit(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "x = 2\n", "two")
    sha = _git(repo, "rev-parse", "HEAD")
    assert scheme.version_for_checkout(str(repo), "0.1.0") == f"0.1.0.dev2+g{sha[:12]}"
