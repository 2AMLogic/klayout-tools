"""The build-time git-identity capture hook (issue #1202).

`hatch_build.py` is what makes an *installed wheel* able to say which commit
it came from -- a wheel has no `.git` directory, so without a build-time
record the question is unanswerable after the fact. It runs in the isolated
build environment (where `hatchling` is present but this package is not), so
these tests load it by path and exercise its plain helpers directly.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_hatch_build():
    spec = importlib.util.spec_from_file_location(
        "klt_hatch_build_under_test", _ROOT / "hatch_build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, "init", "-q", ".")
    (directory / "tracked.py").write_text("x = 1\n")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "initial")


def test_importable_without_hatchling():
    """The helpers must not require the build backend to be installed --
    otherwise they could only ever be exercised inside a real build."""
    module = _load_hatch_build()
    assert hasattr(module, "git_identity")
    assert hasattr(module, "render_build_info")


def test_git_identity_records_tag_and_clean_tree(tmp_path):
    module = _load_hatch_build()
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v9.9.9")

    identity = module.git_identity(str(repo))
    assert identity["tag"] == "v9.9.9"
    assert identity["dirty"] is False
    assert len(identity["commit"]) == 40


def test_git_identity_records_post_tag_and_dirty_state(tmp_path):
    module = _load_hatch_build()
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v9.9.9")
    (repo / "tracked.py").write_text("x = 2\n")

    identity = module.git_identity(str(repo))
    assert identity["dirty"] is True

    _git(repo, "commit", "-qam", "after the tag")
    identity = module.git_identity(str(repo))
    assert identity["tag"] is None
    assert identity["dirty"] is False


def test_git_identity_ignores_untracked_files(tmp_path):
    """A clean, pinned-sha checkout must not read as dirty just because a
    package manager dropped bookkeeping files into the build root (issue
    #2248). Reproduced against a real ``uv --from git+...@<sha>`` install:
    ``uv`` writes its own ``.ok`` checkout-completion sentinel straight into
    the git working tree right after cloning, before the build (and this
    hook) ever runs -- an untracked file with no packaged content that
    nonetheless made ``git status --porcelain`` (which reports untracked
    files by default) flag an immutable revision as ``dirty: true``."""
    module = _load_hatch_build()
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v9.9.9")
    # Simulate the perturbation: an untracked file appears in the build root
    # that was never part of the commit and is not staged for one either.
    (repo / ".ok").write_text("")
    (repo / "some-export-metadata.json").write_text("{}")

    identity = module.git_identity(str(repo))
    assert identity["tag"] == "v9.9.9"
    assert identity["dirty"] is False


def test_git_identity_still_dirty_for_modified_tracked_file_alongside_noise(
    tmp_path,
):
    """Untracked noise must not mask a genuine modification -- a real edit to
    a tracked file still reports ``dirty: true`` even when stray untracked
    files are also present (issue #2248's third acceptance criterion: the
    probe keeps its meaning where it has one)."""
    module = _load_hatch_build()
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / ".ok").write_text("")
    (repo / "tracked.py").write_text("x = 2\n")

    identity = module.git_identity(str(repo))
    assert identity["dirty"] is True


def test_git_identity_is_none_outside_a_repo(tmp_path):
    """A build from an unpacked sdist has no repo -- the hook must then write
    nothing rather than overwrite the record the sdist already carries."""
    module = _load_hatch_build()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert module.git_identity(str(plain)) is None


def test_render_build_info_round_trips(tmp_path):
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info({"commit": "a" * 40, "tag": "v9.9.9", "dirty": False})
    )

    spec = importlib.util.spec_from_file_location("klt_generated_info", generated)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)

    assert loaded.GIT_COMMIT == "a" * 40
    assert loaded.GIT_TAG == "v9.9.9"
    assert loaded.GIT_DIRTY is False


def test_render_build_info_round_trips_with_no_tag(tmp_path):
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info({"commit": "b" * 40, "tag": None, "dirty": None})
    )

    spec = importlib.util.spec_from_file_location(
        "klt_generated_info_no_tag", generated
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)

    assert loaded.GIT_TAG is None
    assert loaded.GIT_DIRTY is None


def test_generated_module_is_consumed_by_build_identity(tmp_path, monkeypatch):
    """End-to-end between the two halves: what the hook writes is exactly what
    `build_identity` reads back out of an installed distribution."""
    from klayout_tools import build_identity

    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info({"commit": "c" * 40, "tag": "v9.9.9", "dirty": False})
    )
    spec = importlib.util.spec_from_file_location(
        "klayout_tools._build_info", generated
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", loaded)

    identity = build_identity._recorded_identity("9.9.9")
    assert identity["git_commit"] == "c" * 40
    assert identity["is_release"] is True


# --------------------------------------------------------------------------- #
# klayout_version_expected (issue #1490)
# --------------------------------------------------------------------------- #


def test_klayout_version_expected_parses_uv_lock(tmp_path):
    module = _load_hatch_build()
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "\n"
        "[[package]]\n"
        'name = "attrs"\n'
        'version = "26.1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        "\n"
        "[[package]]\n"
        'name = "klayout"\n'
        'version = "0.30.10"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    assert module.klayout_version_expected(str(tmp_path)) == "0.30.10"


def test_klayout_version_expected_none_when_lockfile_missing(tmp_path):
    module = _load_hatch_build()
    assert module.klayout_version_expected(str(tmp_path)) is None


def test_klayout_version_expected_none_when_no_klayout_entry(tmp_path):
    module = _load_hatch_build()
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "attrs"\nversion = "26.1.0"\n'
    )
    assert module.klayout_version_expected(str(tmp_path)) is None


def test_klayout_version_expected_matches_this_repos_own_lockfile():
    """Sanity check against the real `uv.lock` at the repo root."""
    module = _load_hatch_build()
    assert module.klayout_version_expected(str(_ROOT)) is not None


def test_render_build_info_includes_klayout_version_expected(tmp_path):
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info(
            {"commit": "a" * 40, "tag": "v9.9.9", "dirty": False}, "0.30.10"
        )
    )

    spec = importlib.util.spec_from_file_location(
        "klt_generated_info_with_klayout", generated
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)

    assert loaded.KLAYOUT_VERSION_EXPECTED == "0.30.10"


def test_render_build_info_klayout_version_expected_defaults_to_none(tmp_path):
    """A caller passing only `identity` (the pre-#1490 call shape) keeps
    working, with `KLAYOUT_VERSION_EXPECTED` recorded as `None`."""
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info({"commit": "b" * 40, "tag": None, "dirty": None})
    )

    spec = importlib.util.spec_from_file_location(
        "klt_generated_info_no_klayout", generated
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)

    assert loaded.KLAYOUT_VERSION_EXPECTED is None


def test_pyproject_registers_the_build_hook():
    """Without this registration the hook never runs, and every wheel would
    silently report an unknown identity.

    Asserted against the raw text rather than a parsed table so the check runs
    on 3.10 too (`tomllib` is stdlib only from 3.11, PEP 680) -- a
    registration this load-bearing must not be verified on some interpreters
    only.
    """
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.hatch.build.hooks.custom]" in pyproject
    assert 'path = "hatch_build.py"' in pyproject


def test_pyproject_computes_the_version_at_build_time():
    """Issue #2823: the distribution version must be computed, not static --
    a static `version` is exactly what let a post-tag build declare `X.Y.Z`
    and satisfy a `==X.Y.Z` pin."""
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not re.search(r'(?m)^version = "', pyproject)
    assert re.search(r'(?m)^dynamic = \[.*"version"', pyproject)
    table = pyproject.split("\n[tool.hatch.version]\n", 1)[1].split("\n[", 1)[0]
    assert 'source = "code"' in table
    assert 'path = "hatch_build.py"' in table
    assert 'expression = "version_from_pyproject()"' in table
    assert re.search(r'(?m)^base-version = "\d+\.\d+\.\d+"', table)


def test_render_build_info_records_the_version(tmp_path):
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    generated.write_text(
        module.render_build_info(
            {"commit": "a" * 40, "tag": None, "dirty": False},
            None,
            None,
            "9.9.10.dev1+gaaaaaaaaaaaa",
        )
    )
    spec = importlib.util.spec_from_file_location("klt_generated_version", generated)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.VERSION == "9.9.10.dev1+gaaaaaaaaaaaa"


def test_distribution_version_from_a_checkout(tmp_path):
    module = _load_hatch_build()
    repo = tmp_path / "repo"
    _make_repo(repo)
    _git(repo, "tag", "v9.9.9")
    _copy_version_scheme(repo)
    # The scheme module is untracked here; untracked files never count as
    # dirty, so this is still the clean release.
    assert module.distribution_version(str(repo), "9.9.9") == "9.9.9"


def test_distribution_version_carries_the_sdist_record_forward(tmp_path):
    """An unpacked sdist has no `.git`; the version its record carries wins
    over anything the missing git state could suggest."""
    module = _load_hatch_build()
    root = tmp_path / "sdist"
    _copy_version_scheme(root)
    (root / "src" / "klayout_tools" / "_build_info.py").write_text(
        module.render_build_info(
            {"commit": "a" * 40, "tag": None, "dirty": False},
            version="9.9.10.dev4+gaaaaaaaaaaaa",
        )
    )
    assert module.recorded_version(str(root)) == "9.9.10.dev4+gaaaaaaaaaaaa"
    assert module.distribution_version(str(root), "9.9.9") == (
        "9.9.10.dev4+gaaaaaaaaaaaa"
    )


def test_distribution_version_without_git_or_record_is_never_the_release(tmp_path):
    module = _load_hatch_build()
    root = tmp_path / "tarball"
    _copy_version_scheme(root)
    assert module.recorded_version(str(root)) is None
    assert module.distribution_version(str(root), "9.9.9") == "9.9.9.dev0+unknown"


def test_version_from_pyproject_for_this_repo():
    """The exact expression hatchling evaluates, against this checkout."""
    packaging_version = __import__("pytest").importorskip("packaging.version")
    module = _load_hatch_build()
    version = module.version_from_pyproject(str(_ROOT))
    assert str(packaging_version.Version(version)) == version


def _copy_version_scheme(root: Path) -> None:
    package = root / "src" / "klayout_tools"
    package.mkdir(parents=True, exist_ok=True)
    source = _ROOT / "src" / "klayout_tools" / "_version_scheme.py"
    (package / "_version_scheme.py").write_text(source.read_text(encoding="utf-8"))


def test_deck_hashes_records_each_deck_module(tmp_path):
    import hashlib

    module = _load_hatch_build()
    decks = tmp_path / "src" / "klayout_tools" / "decks"
    decks.mkdir(parents=True)
    (decks / "sky130.py").write_bytes(b"DECK = []\n")
    (decks / "notes.txt").write_text("ignored")

    assert module.deck_hashes(str(tmp_path)) == {
        "sky130": "sha256:" + hashlib.sha256(b"DECK = []\n").hexdigest()
    }
    assert module.deck_hashes(str(tmp_path / "missing")) is None


def test_render_build_info_includes_deck_hashes(tmp_path):
    module = _load_hatch_build()
    generated = tmp_path / "_build_info.py"
    hashes = {"sky130": "sha256:" + "e" * 64}
    generated.write_text(
        module.render_build_info(
            {"commit": "a" * 40, "tag": "v9.9.9", "dirty": False}, None, hashes
        )
    )
    spec = importlib.util.spec_from_file_location(
        "klt_generated_deck_hashes", generated
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.DECK_HASHES == hashes
