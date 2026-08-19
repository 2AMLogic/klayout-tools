"""Build identity of the running klt build (issue #1202).

The gap these cover: a wheel built from a commit *after* a release tag
declares the same package version the release does, so `klt --version` alone
could not tell a consumer committing klt output as evidence which build
produced it. `build_identity` adds the missing half -- a PEP 440
local-version suffix for anything that is not a confirmed tagged release --
without changing what a real release reports.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import __version__, build_identity
from klayout_tools.cli import main

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
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
    )


def _make_repo(directory: Path) -> None:
    """A git checkout with one tracked, committed file."""
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, "init", "-q", ".")
    (directory / "tracked.py").write_text("x = 1\n")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "initial")


# --------------------------------------------------------------------------- #
# version-string rendering
# --------------------------------------------------------------------------- #


def test_confirmed_release_renders_bare_version():
    """Regression guard: existing consumers parse `klt X.Y.Z`, so a real
    tagged-release build must gain no suffix at all."""
    ident = {
        "git_commit": "a" * 40,
        "git_tag": "v1.2.3",
        "dirty": False,
        "is_release": True,
    }
    assert build_identity.format_build_version("1.2.3", ident) == "1.2.3"


def test_post_tag_build_renders_local_version_segment():
    ident = {
        "git_commit": "abcdef0123456789" + "0" * 24,
        "git_tag": None,
        "dirty": False,
        "is_release": False,
    }
    assert build_identity.format_build_version("1.2.3", ident) == "1.2.3+gabcdef012345"


def test_dirty_build_is_marked_dirty():
    ident = {
        "git_commit": "abcdef0123456789" + "0" * 24,
        "git_tag": "v1.2.3",
        "dirty": True,
        "is_release": False,
    }
    assert (
        build_identity.format_build_version("1.2.3", ident)
        == "1.2.3+gabcdef012345.dirty"
    )


def test_unrecoverable_identity_renders_unknown_not_bare():
    """The whole point: "I cannot tell" must never look like a release."""
    ident = {
        "git_commit": None,
        "git_tag": None,
        "dirty": None,
        "is_release": None,
    }
    rendered = build_identity.format_build_version("1.2.3", ident)
    assert rendered == "1.2.3+unknown"
    assert rendered != "1.2.3"


# --------------------------------------------------------------------------- #
# release resolution (tri-state)
# --------------------------------------------------------------------------- #


def test_resolve_release_requires_matching_tag_and_clean_tree():
    assert build_identity._resolve("a" * 40, "v1.2.3", False, "1.2.3")["is_release"]
    # Unprefixed tag style is accepted too.
    assert build_identity._resolve("a" * 40, "1.2.3", False, "1.2.3")["is_release"]


def test_resolve_rejects_tag_for_a_different_version():
    ident = build_identity._resolve("a" * 40, "v0.9.0", False, "1.2.3")
    assert ident["is_release"] is False


def test_resolve_rejects_dirty_tree_at_a_release_tag():
    ident = build_identity._resolve("a" * 40, "v1.2.3", True, "1.2.3")
    assert ident["is_release"] is False


def test_resolve_reports_unknown_not_false_without_any_git_facts():
    # Tri-state, mirroring `provenance.deck.released`: an unanswerable
    # question must not be reported as a confirmed "not a release".
    assert build_identity._resolve(None, None, None, "1.2.3")["is_release"] is None


# --------------------------------------------------------------------------- #
# live checkout probing
# --------------------------------------------------------------------------- #


def test_checkout_identity_reports_release_at_matching_tag(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v1.2.3")

    ident = build_identity._checkout_identity(str(repo), package_version="1.2.3")
    assert ident["is_release"] is True
    assert ident["git_tag"] == "v1.2.3"
    assert ident["dirty"] is False
    assert len(ident["git_commit"]) == 40


def test_checkout_identity_reports_non_release_after_the_tag(tmp_path):
    """The exact reported scenario: a source build made from a commit after a
    release tag must be distinguishable from the release itself."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v1.2.3")
    tagged = build_identity._checkout_identity(str(repo), package_version="1.2.3")

    (repo / "tracked.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "after the tag")
    post_tag = build_identity._checkout_identity(str(repo), package_version="1.2.3")

    assert post_tag["is_release"] is False
    assert post_tag["git_tag"] is None
    assert post_tag["git_commit"] != tagged["git_commit"]
    assert build_identity.format_build_version(
        "1.2.3", post_tag
    ) != build_identity.format_build_version("1.2.3", tagged)


def test_checkout_identity_detects_uncommitted_changes(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v1.2.3")
    (repo / "tracked.py").write_text("x = 999\n")

    ident = build_identity._checkout_identity(str(repo), package_version="1.2.3")
    assert ident["dirty"] is True
    assert ident["is_release"] is False


def test_checkout_identity_unknown_outside_a_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    ident = build_identity._checkout_identity(str(plain), package_version="1.2.3")
    assert ident == {
        "git_commit": None,
        "git_tag": None,
        "dirty": None,
        "is_release": None,
    }


def test_checkout_identity_ignores_an_untracked_dir_inside_a_repo(tmp_path):
    """A non-editable install into a `.venv/` that happens to sit inside some
    unrelated checkout must not inherit that checkout's HEAD."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    site_packages = repo / ".venv" / "lib" / "site-packages" / "klayout_tools"
    site_packages.mkdir(parents=True)
    (site_packages / "__init__.py").write_text("")

    ident = build_identity._checkout_identity(
        str(site_packages), package_version="1.2.3"
    )
    assert ident["git_commit"] is None
    assert ident["is_release"] is None


# --------------------------------------------------------------------------- #
# build-time record (what an installed wheel carries)
# --------------------------------------------------------------------------- #


def test_recorded_identity_is_preferred_over_a_live_probe(monkeypatch):
    """An installed wheel's origin is fixed at build time; a checkout its
    files merely happen to sit in can drift or belong to another project."""
    module = type(sys)("klayout_tools._build_info")
    module.GIT_COMMIT = "b" * 40
    module.GIT_TAG = f"v{__version__}"
    module.GIT_DIRTY = False
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)

    def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("live git probe ran despite a build-time record")

    monkeypatch.setattr(build_identity, "_checkout_identity", _fail)

    ident = build_identity.identity()
    assert ident["git_commit"] == "b" * 40
    assert ident["is_release"] is True
    assert build_identity.build_version() == __version__


def test_recorded_identity_absent_falls_back_to_the_checkout(monkeypatch):
    monkeypatch.setattr(build_identity, "_recorded_identity", lambda _v: None)
    sentinel = {
        "git_commit": "c" * 40,
        "git_tag": None,
        "dirty": False,
        "is_release": False,
    }
    monkeypatch.setattr(build_identity, "_checkout_identity", lambda: sentinel)

    assert build_identity.identity() == sentinel


def test_recorded_identity_tolerates_a_malformed_record(monkeypatch):
    module = type(sys)("klayout_tools._build_info")
    module.GIT_COMMIT = ""
    module.GIT_TAG = 17
    module.GIT_DIRTY = "nope"
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)

    ident = build_identity._recorded_identity("1.2.3")
    assert ident == {
        "git_commit": None,
        "git_tag": None,
        "dirty": None,
        "is_release": None,
    }


def test_git_helper_never_raises_when_git_is_missing(monkeypatch, tmp_path):
    def _raise(*args, **kwargs):
        raise OSError("git not installed")

    monkeypatch.setattr(build_identity.subprocess, "run", _raise)
    assert build_identity._git(str(tmp_path), "rev-parse", "HEAD") is None


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_version_report_shape():
    report = build_identity.version_report()
    assert set(report) == {
        "schema_version",
        "version",
        "package_version",
        "git_commit",
        "git_tag",
        "dirty",
        "is_release",
    }
    assert report["schema_version"] == 1
    assert report["package_version"] == __version__
    assert report["version"].startswith(__version__)
    assert report["is_release"] in (True, False, None)


def test_cli_version_json(capsys):
    assert main(["version", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == build_identity.version_report()


def test_cli_version_text(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"klt {build_identity.build_version()}"


def test_cli_version_flag_matches_the_version_verb(monkeypatch, capsys):
    monkeypatch.setattr(build_identity, "_recorded_identity", lambda _v: None)
    monkeypatch.setattr(
        build_identity,
        "_checkout_identity",
        lambda: {
            "git_commit": "d" * 40,
            "git_tag": None,
            "dirty": False,
            "is_release": False,
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    flag_output = capsys.readouterr().out.strip()

    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == flag_output
    assert flag_output == f"klt {__version__}+g{'d' * 12}"


def test_cli_version_flag_is_bare_for_a_release_build(monkeypatch, capsys):
    """A real `pip install klayout-tools==X.Y.Z` must keep printing exactly
    `klt X.Y.Z` -- no suffix, no new field to parse around."""
    monkeypatch.setattr(
        build_identity,
        "_recorded_identity",
        lambda version: {
            "git_commit": "e" * 40,
            "git_tag": f"v{version}",
            "dirty": False,
            "is_release": True,
        },
    )

    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"klt {__version__}"


def test_version_flag_does_not_probe_git_for_unrelated_commands(monkeypatch):
    """`--version` is resolved lazily: building the parser for some other
    command must not shell out to git on every klt invocation."""
    from klayout_tools.cli.parser import create_parser

    def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("git probed while merely building the parser")

    monkeypatch.setattr(build_identity.subprocess, "run", _fail)
    create_parser().parse_args(["deck", "hash", "--deck", "sky130"])
