"""Built-artifact versions and exact-pin resolution (issue #2823).

The unit tests in `test_version_scheme.py` prove the policy; these prove the
*artifacts*: a real `uv build` of a throwaway checkout carrying this repo's
own `[tool.hatch.version]` configuration, `hatch_build.py`, and version /
identity modules. They check the `METADATA` each wheel and sdist actually
declares, that the sdist-to-wheel path (no `.git`) keeps the originating
version, and -- the bug itself -- that an exact `==X.Y.Z` request against a
local index holding both a clean-release and a post-tag build selects only
the release, whose every runtime surface then agrees.

Skipped when `uv` is unavailable. The builds need `hatchling`, which `uv`
fetches into its isolated build environment (from its cache when warm).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(_UV is None, reason="needs the `uv` CLI")

packaging_version = pytest.importorskip("packaging.version")
packaging_specifiers = pytest.importorskip("packaging.specifiers")

_BASE = "9.9.9"
#: Console script (and the module backing it) the throwaway project installs,
#: so the launcher test can run the installed artifact the way `klt` runs.
_SCRIPT = "klt-identity-probe"
_PROBE_MODULE = "_identity_probe"
_PACKAGE_FILES = ("__init__.py", "_version_scheme.py", "build_identity.py")


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )
    assert completed.returncode == 0, (
        f"{args} failed ({completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout.strip()


def _uv_env(cache: Path) -> dict[str, str]:
    """Environment running the `uv pip install` steps against a private
    ``UV_CACHE_DIR`` (issue #3186).

    uv's wheel cache is keyed by the wheel's path and name, not its content.
    Every session here builds a ``klayout_tools-9.9.9-py3-none-any.whl`` whose
    *content* (the embedded git commit) differs, under a basetemp path that
    repeats across runs on a fresh CI runner (``/tmp/pytest-of-runner/
    pytest-0/...``) -- so with the ``UV_CACHE_DIR`` that setup-uv persists
    across CI runs, the install could serve the *previous* session's unpacked
    wheel and the identity probe would report the previous session's
    throwaway commit. Pointing these steps at a directory inside this
    session's own tmp tree makes every install unpack the wheel that was
    actually just built.
    """
    return {**os.environ, "UV_CACHE_DIR": str(cache)}


def _git(repo: Path, *args: str) -> str:
    return _run(
        "git",
        "-C",
        str(repo),
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        *args,
    )


def _table(pyproject: str, header: str) -> str:
    """`header` plus its body, verbatim from this repo's `pyproject.toml`."""
    body = pyproject.split(f"\n{header}\n", 1)[1].split("\n[", 1)[0]
    return f"{header}\n{body.rstrip()}\n"


def _make_project(directory: Path) -> Path:
    """A minimal checkout of this package: the real version/identity code
    and build configuration, none of the rest."""
    real = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_table = _table(real, "[tool.hatch.version]")
    version_table = version_table.replace(
        version_table.split("base-version = ", 1)[1].split("\n", 1)[0],
        f'"{_BASE}"',
    )
    pyproject = "\n".join(
        [
            "[project]",
            'name = "klayout-tools"',
            'dynamic = ["version"]',
            'requires-python = ">=3.10"',
            "",
            "[project.scripts]",
            f'{_SCRIPT} = "klayout_tools.{_PROBE_MODULE}:main"',
            "",
            _table(real, "[build-system]"),
            _table(real, "[tool.hatch.build.targets.wheel]"),
            version_table,
            _table(real, "[tool.hatch.build.hooks.custom]"),
        ]
    )
    package = directory / "src" / "klayout_tools"
    package.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    shutil.copy(_ROOT / "hatch_build.py", directory / "hatch_build.py")
    for name in _PACKAGE_FILES:
        shutil.copy(_ROOT / "src" / "klayout_tools" / name, package / name)
    (package / f"{_PROBE_MODULE}.py").write_text(
        f"def main():\n{textwrap.indent(_PROBE, '    ')}\n", encoding="utf-8"
    )
    (directory / ".gitignore").write_text("dist/\n")
    _git(directory, "init", "-q", ".")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-qm", "initial")
    return directory


def _build(project: Path, out_dir: Path, *extra: str) -> None:
    _run(_UV, "build", "--quiet", "--out-dir", str(out_dir), *extra, str(project))


def _wheel(out_dir: Path) -> Path:
    (wheel,) = out_dir.glob("*.whl")
    return wheel


def _sdist(out_dir: Path) -> Path:
    (sdist,) = out_dir.glob("*.tar.gz")
    return sdist


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        metadata = archive.read(name).decode("utf-8")
    return _metadata_field(metadata, "Version")


def _sdist_version(sdist: Path) -> str:
    with tarfile.open(sdist) as archive:
        (member,) = [
            m
            for m in archive.getmembers()
            if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")
        ]
        metadata = archive.extractfile(member).read().decode("utf-8")
    return _metadata_field(metadata, "Version")


def _metadata_field(metadata: str, field: str) -> str:
    for line in metadata.splitlines():
        if line.startswith(f"{field}: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"no {field} in metadata")


def _pins(version: str) -> bool:
    return packaging_specifiers.SpecifierSet(f"=={_BASE}").contains(
        packaging_version.Version(version), prereleases=True
    )


_PROBE = """
import importlib.metadata, json
import klayout_tools
from klayout_tools import build_identity
report = build_identity.version_report()
print(json.dumps({
    "metadata": importlib.metadata.version("klayout-tools"),
    "dunder": klayout_tools.__version__,
    "build_version": build_identity.build_version(),
    "version": report["version"],
    "package_version": report["package_version"],
    "is_release": report["is_release"],
    "git_commit": report["git_commit"],
}))
"""


def _install_and_probe(tmp: Path, links: Path, requirement: str) -> dict:
    uv_env = _uv_env(tmp / "uv-cache")
    venv = tmp / "venv"
    _run(
        _UV,
        "venv",
        "--quiet",
        "--python",
        sys.executable,
        str(venv),
        env=uv_env,
    )
    python = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    # `--cache-dir` scoped to this call's own scratch space: a uv cache shared
    # across sessions (CI restores one) serves same-named find-links wheels
    # from a path-keyed unpack, and every wheel this module builds lands at a
    # path CI reuses verbatim (`/tmp/pytest-of-runner/pytest-0/...`) — so a
    # restored cache can install a *previous* run's release wheel, whose
    # build record carries that run's throwaway-repo commit. Same isolation
    # `test_one_artifact_reports_one_identity_under_every_launcher` already
    # gives its uvx/uv-run probes.
    _run(
        _UV,
        "pip",
        "install",
        "--quiet",
        "--python",
        str(python),
        "--no-index",
        "--find-links",
        str(links),
        "--cache-dir",
        str(tmp / "uv-cache"),
        "--no-deps",
        requirement,
        env=uv_env,
    )
    # `-I`: isolated mode, so the probe imports the installed artifact, never
    # this checkout's own `src/`.
    return json.loads(_run(str(python), "-I", "-c", _PROBE, cwd=tmp))


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    """Builds of one throwaway project: at its release tag, then one commit
    past it. `uv build` (no target flags) builds the wheel *from the sdist*,
    so each wheel here already exercises the no-`.git` sdist-to-wheel path."""
    tmp = tmp_path_factory.mktemp("artifacts")
    project = _make_project(tmp / "project")

    _git(project, "tag", f"v{_BASE}")
    release_dir = tmp / "dist-release"
    _build(project, release_dir)
    release_commit = _git(project, "rev-parse", "HEAD")

    (project / "src" / "klayout_tools" / "later.py").write_text("X = 1\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "after the release")
    dev_dir = tmp / "dist-dev"
    _build(project, dev_dir)
    dev_commit = _git(project, "rev-parse", "HEAD")

    links = tmp / "links"
    links.mkdir()
    for wheel in (_wheel(release_dir), _wheel(dev_dir)):
        shutil.copy(wheel, links / wheel.name)

    return {
        "tmp": tmp,
        "release": release_dir,
        "dev": dev_dir,
        "links": links,
        "release_commit": release_commit,
        "dev_commit": dev_commit,
    }


def test_clean_tagged_build_declares_the_bare_release(artifacts):
    assert _wheel_version(_wheel(artifacts["release"])) == _BASE
    assert _sdist_version(_sdist(artifacts["release"])) == _BASE


def test_post_tag_build_declares_a_distinct_public_version(artifacts):
    expected = f"9.9.10.dev1+g{artifacts['dev_commit'][:12]}"
    wheel_version = _wheel_version(_wheel(artifacts["dev"]))
    assert wheel_version == expected
    assert _sdist_version(_sdist(artifacts["dev"])) == expected
    assert not _pins(wheel_version)
    assert packaging_version.Version(wheel_version) > packaging_version.Version(_BASE)


def test_wheel_rebuilt_from_an_unpacked_sdist_keeps_its_version(artifacts, tmp_path):
    """Even with `PKG-INFO` removed (so hatchling cannot reuse it), the
    record the sdist carries preserves the originating checkout's version."""
    with tarfile.open(_sdist(artifacts["dev"])) as archive:
        # `filter=` exists from 3.10.12/3.11.4 on; older interpreters ignore
        # it here (the archive is one this test just built).
        kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        archive.extractall(tmp_path / "unpacked", **kwargs)
    (unpacked,) = (tmp_path / "unpacked").iterdir()
    assert not (unpacked / ".git").exists()
    (unpacked / "PKG-INFO").unlink()

    out = tmp_path / "rebuilt"
    _build(unpacked, out, "--wheel")
    assert _wheel_version(_wheel(out)) == _wheel_version(_wheel(artifacts["dev"]))


def test_exact_pin_selects_only_the_tagged_release(artifacts, tmp_path):
    """The reported bug: with a post-tag build available to the resolver, an
    exact `==X.Y.Z` request must still install -- and run -- the release."""
    probe = _install_and_probe(tmp_path, artifacts["links"], f"klayout-tools=={_BASE}")
    assert probe == {
        "metadata": _BASE,
        "dunder": _BASE,
        "build_version": _BASE,
        "version": _BASE,
        "package_version": _BASE,
        "is_release": True,
        "git_commit": artifacts["release_commit"],
    }


def test_post_tag_build_reports_one_identity_on_every_surface(artifacts, tmp_path):
    expected = _wheel_version(_wheel(artifacts["dev"]))
    probe = _install_and_probe(
        tmp_path, artifacts["links"], f"klayout-tools=={expected}"
    )
    assert {
        probe[k]
        for k in ("metadata", "dunder", "build_version", "version", "package_version")
    } == {expected}
    assert probe["is_release"] is False
    assert probe["git_commit"] == artifacts["dev_commit"]


def test_one_artifact_reports_one_identity_under_every_launcher(artifacts, tmp_path):
    """Running the same installed wheel via its console script, `uvx`, and
    `uv run --no-project --with` reports the same identity: it comes from the
    artifact's metadata and build record, never from how it was launched."""
    expected = _wheel_version(_wheel(artifacts["dev"]))
    requirement = f"klayout-tools=={expected}"
    venv = tmp_path / "venv"
    _install_and_probe(venv.parent, artifacts["links"], requirement)
    script = venv / ("Scripts" if sys.platform == "win32" else "bin") / _SCRIPT

    offline = (
        "--python", sys.executable,
        "--no-index", "--find-links", str(artifacts["links"]),
        "--cache-dir", str(tmp_path / "uv-cache"),
    )  # fmt: skip
    runs = {
        "console_script": _run(str(script), cwd=tmp_path),
        "uvx": _run(
            _UV, "tool", "run", *offline, "--from", requirement, _SCRIPT,
            cwd=tmp_path,
        ),
        "uv_run_no_project": _run(
            _UV, "run", "--no-project", *offline, "--with", requirement,
            _SCRIPT, cwd=tmp_path,
        ),
    }  # fmt: skip
    reports = {name: json.loads(out) for name, out in runs.items()}
    assert reports["console_script"]["version"] == expected
    assert reports["console_script"]["git_commit"] == artifacts["dev_commit"]
    for name, report in reports.items():
        assert report == reports["console_script"], name
