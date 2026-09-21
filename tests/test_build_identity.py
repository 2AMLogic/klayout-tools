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

from klayout_tools import __version__, _provenance, build_identity
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
# provenance wiring (issue #2090)
# --------------------------------------------------------------------------- #


def _assert_provenance_matches_cli(expected: str, capsys) -> None:
    assert _provenance.build_provenance()["klt_version"] == expected
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"klt {expected}"
    assert main(["version", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == expected
    # Package metadata keeps its separate, static-version contract.
    assert payload["package_version"] == __version__


@pytest.mark.parametrize("state", ["release", "post-tag", "dirty", "unknown"])
def test_provenance_identifies_source_build(state, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "source"
    if state == "unknown":
        repo.mkdir()
        expected = f"{__version__}+unknown"
    else:
        _make_repo(repo)
        _git(repo, "tag", f"v{__version__}")
        if state in {"post-tag", "dirty"}:
            (repo / "tracked.py").write_text("x = 2\n")
        if state == "post-tag":
            _git(repo, "commit", "-qam", "after the tag")
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        expected = __version__
        if state != "release":
            expected += f"+g{commit[:12]}"
        if state == "dirty":
            expected += ".dirty"

    monkeypatch.setattr(build_identity, "_recorded_identity", lambda _v: None)
    monkeypatch.setattr(build_identity, "__file__", str(repo / "tracked.py"))
    _assert_provenance_matches_cli(expected, capsys)


@pytest.mark.parametrize(
    "tagged,dirty,commit,suffix",
    [
        (True, False, "b" * 40, ""),
        (False, False, "b" * 40, "+g" + "b" * 12),
        (True, True, "b" * 40, "+g" + "b" * 12 + ".dirty"),
        (False, None, None, "+unknown"),
    ],
    ids=["release", "post-tag", "dirty", "unknown"],
)
def test_provenance_identifies_packaged_build(
    tagged, dirty, commit, suffix, monkeypatch, capsys
):
    module = type(sys)("klayout_tools._build_info")
    module.GIT_COMMIT = commit
    module.GIT_TAG = f"v{__version__}" if tagged else None
    module.GIT_DIRTY = dirty
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)

    def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("packaged provenance must not probe the checkout")

    monkeypatch.setattr(build_identity, "_checkout_identity", _fail)
    _assert_provenance_matches_cli(f"{__version__}{suffix}", capsys)


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
        "grading_ruleset_id",
        "klayout_version",
        "klayout_version_expected",
    }
    assert report["schema_version"] == 1
    assert report["package_version"] == __version__
    assert report["version"].startswith(__version__)
    assert report["is_release"] in (True, False, None)
    assert report["grading_ruleset_id"].startswith("sha256:")
    assert report["klayout_version"] is None or isinstance(
        report["klayout_version"], str
    )
    assert report["klayout_version_expected"] is None or isinstance(
        report["klayout_version_expected"], str
    )


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


# --------------------------------------------------------------------------- #
# klayout_version_expected (issue #1490)
# --------------------------------------------------------------------------- #


def test_recorded_klayout_version_expected_reads_build_info(monkeypatch):
    module = type(sys)("klayout_tools._build_info")
    module.KLAYOUT_VERSION_EXPECTED = "0.30.10"
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)

    assert build_identity._recorded_klayout_version_expected() == "0.30.10"


def test_recorded_klayout_version_expected_none_without_build_info(monkeypatch):
    monkeypatch.delitem(sys.modules, "klayout_tools._build_info", raising=False)
    # This editable/source checkout carries no `_build_info` module at all
    # (see this file's own docstring), so the real import fails -- the same
    # `ImportError` path `_recorded_identity` already exercises.
    assert build_identity._recorded_klayout_version_expected() is None


def test_recorded_klayout_version_expected_tolerates_a_malformed_record(monkeypatch):
    module = type(sys)("klayout_tools._build_info")
    module.KLAYOUT_VERSION_EXPECTED = 17  # wrong type -- never fabricate a string
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)

    assert build_identity._recorded_klayout_version_expected() is None


def test_checkout_klayout_version_expected_reads_uv_lock(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "uv.lock").write_text(
        '[[package]]\nname = "klayout"\nversion = "0.30.10"\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add uv.lock")

    assert build_identity._checkout_klayout_version_expected(str(repo)) == "0.30.10"


def test_checkout_klayout_version_expected_none_without_uv_lock(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)

    assert build_identity._checkout_klayout_version_expected(str(repo)) is None


def test_checkout_klayout_version_expected_none_outside_a_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert build_identity._checkout_klayout_version_expected(str(plain)) is None


def test_checkout_klayout_version_expected_ignores_untracked_venv(tmp_path):
    """Same guard as `_checkout_identity`: a `.venv/` sitting inside some
    unrelated checkout must not inherit that checkout's `uv.lock`."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "uv.lock").write_text(
        '[[package]]\nname = "klayout"\nversion = "0.30.10"\n'
    )
    site_packages = repo / ".venv" / "lib" / "site-packages" / "klayout_tools"
    site_packages.mkdir(parents=True)
    (site_packages / "__init__.py").write_text("")

    assert build_identity._checkout_klayout_version_expected(str(site_packages)) is None


def test_klayout_version_expected_prefers_recorded_over_live_probe(monkeypatch):
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.10"
    )

    def _fail():  # pragma: no cover - must not be reached
        raise AssertionError("live probe ran despite a build-time record")

    monkeypatch.setattr(build_identity, "_checkout_klayout_version_expected", _fail)

    assert build_identity.klayout_version_expected() == "0.30.10"


def test_klayout_version_expected_falls_back_to_live_probe(monkeypatch):
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: None
    )
    monkeypatch.setattr(
        build_identity, "_checkout_klayout_version_expected", lambda: "0.30.9"
    )

    assert build_identity.klayout_version_expected() == "0.30.9"


def test_version_report_includes_klayout_fields(monkeypatch):
    monkeypatch.setattr(build_identity, "_klayout_version", lambda: "0.30.12")
    monkeypatch.setattr(build_identity, "klayout_version_expected", lambda: "0.30.10")

    report = build_identity.version_report()
    assert report["klayout_version"] == "0.30.12"
    assert report["klayout_version_expected"] == "0.30.10"


# --------------------------------------------------------------------------- #
# grading_ruleset_id (issue #2216) -- distinguishing installs that claim the
# same `klt` version but ship different `signoff.py` grading rules.
# --------------------------------------------------------------------------- #


def test_grading_ruleset_id_is_a_sha256_prefixed_hash():
    ruleset_id = build_identity.grading_ruleset_id()
    assert ruleset_id is not None
    assert ruleset_id.startswith("sha256:")
    assert len(ruleset_id) == len("sha256:") + 64


def test_grading_ruleset_id_is_stable_across_calls():
    """Same running build, called twice -- must not fabricate churn."""
    assert build_identity.grading_ruleset_id() == build_identity.grading_ruleset_id()


def test_grading_ruleset_id_identical_for_byte_identical_content_at_a_different_path(
    tmp_path,
):
    """AC: a registry wheel and a `git+...` snapshot of the same tag ship
    byte-identical `signoff.py` source at different install paths (neither
    has the other's `.git` history to compare against) -- the id must match
    regardless of where the file physically lives."""
    real_path = build_identity._grading_module_path()
    content = Path(real_path).read_bytes()

    copy_a = tmp_path / "install_a" / "signoff.py"
    copy_b = tmp_path / "install_b" / "signoff.py"
    copy_a.parent.mkdir()
    copy_b.parent.mkdir()
    copy_a.write_bytes(content)
    copy_b.write_bytes(content)

    def _hash_at(path: Path) -> str | None:
        monkeypatch_target = str(path)
        original = build_identity._grading_module_path
        build_identity._grading_module_path = lambda: monkeypatch_target
        try:
            return build_identity.grading_ruleset_id()
        finally:
            build_identity._grading_module_path = original

    id_a = _hash_at(copy_a)
    id_b = _hash_at(copy_b)
    assert id_a == id_b == build_identity.grading_ruleset_id()


def test_grading_ruleset_id_changes_when_the_grading_module_content_changes(
    tmp_path, monkeypatch
):
    """AC: the identifier changes when `signoff.py`'s grading logic changes."""
    module_copy = tmp_path / "signoff.py"
    module_copy.write_text("# grading logic, version A\n")
    monkeypatch.setattr(
        build_identity, "_grading_module_path", lambda: str(module_copy)
    )
    before = build_identity.grading_ruleset_id()

    module_copy.write_text("# grading logic, version B -- a rule changed\n")
    after = build_identity.grading_ruleset_id()

    assert before != after
    assert before is not None and before.startswith("sha256:")
    assert after is not None and after.startswith("sha256:")


def test_grading_ruleset_id_none_when_the_module_is_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist" / "signoff.py"
    monkeypatch.setattr(build_identity, "_grading_module_path", lambda: str(missing))
    assert build_identity.grading_ruleset_id() is None


def test_version_report_includes_grading_ruleset_id(monkeypatch):
    monkeypatch.setattr(
        build_identity, "grading_ruleset_id", lambda: "sha256:" + "a" * 64
    )
    report = build_identity.version_report()
    assert report["grading_ruleset_id"] == "sha256:" + "a" * 64


def test_version_flag_does_not_probe_git_for_unrelated_commands(monkeypatch):
    """`--version` is resolved lazily: building the parser for some other
    command must not shell out to git on every klt invocation."""
    from klayout_tools.cli.parser import create_parser

    def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("git probed while merely building the parser")

    monkeypatch.setattr(build_identity.subprocess, "run", _fail)
    create_parser().parse_args(["deck", "hash", "--deck", "sky130"])
