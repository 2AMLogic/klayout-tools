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
    assert build_identity.format_build_version("9.9.9", identity) == "9.9.9"


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
    # The package version stays static: this adds a build identity beside it,
    # it does not make the package version dynamic (a real
    # `pip install klayout-tools==X.Y.Z` must keep reporting bare `X.Y.Z`).
    assert re.search(r'(?m)^version = "\d', pyproject)
    assert not re.search(r'(?m)^dynamic = .*"version"', pyproject)
