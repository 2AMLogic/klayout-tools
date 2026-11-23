"""Read PSP103 device currents through the public sim CLI with real ngspice."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools.cli import main

VARIANT = Path("pdks/ihp-open-pdk/ihp-sg13g2")
CARD = Path("libs.tech/ngspice/models/cornerMOSlv.lib")
MODEL = Path("libs.tech/ngspice/osdi/psp103.osdi")


def _shared_checkout(repo: Path) -> Path | None:
    """Root of the main checkout when ``repo`` is a linked git worktree.

    Git's own metadata names the main checkout, so this holds wherever the
    worktree lives (``.loom/worktrees/<name>``, ``.loom/worktrees/repo/pr-<N>``).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    common = (repo / out).resolve()
    return common.parent if out and common.name == ".git" else None


def _psp103_files(repo: Path, override: str | None) -> tuple[Path, Path] | None:
    if override is not None:
        roots = [Path(override).resolve()]
    else:
        roots = [repo / VARIANT]
        shared = _shared_checkout(repo)
        if shared is not None and shared != repo.resolve():
            roots.append(shared / VARIANT)
    for root in roots:
        assets = root / "libs.tech/ngspice"
        card = assets / "models/cornerMOSlv.lib"
        model = assets / "osdi/psp103.osdi"
        if card.is_file() and model.is_file():
            return card, model
    return None


def _git(cwd: Path, *args: str) -> None:
    identity = ["-c", "user.name=klt", "-c", "user.email=klt@example.invalid"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


def _touch(root: Path, *files: Path) -> None:
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).touch()


@pytest.fixture
def main_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("requires git")
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "-q")
    _git(main_repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "init")
    return main_repo


@pytest.mark.parametrize(
    "layout", [".loom/worktrees/worker", ".loom/worktrees/repo/pr-7250"]
)
def test_psp103_discovery_in_a_loom_worktree(main_checkout, monkeypatch, layout):
    repo = main_checkout / layout
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    local, shared = repo / VARIANT, main_checkout / VARIANT
    assert _psp103_files(repo, None) is None
    _touch(shared, CARD, MODEL)
    assert _psp103_files(repo, None) == (shared / CARD, shared / MODEL)
    _touch(local, CARD)  # an incomplete local install still falls back
    assert _psp103_files(repo, None) == (shared / CARD, shared / MODEL)
    _touch(local, MODEL)
    assert _psp103_files(repo, None) == (local / CARD, local / MODEL)
    monkeypatch.chdir(main_checkout)
    assert _psp103_files(repo, str(VARIANT)) == (shared / CARD, shared / MODEL)


def test_psp103_discovery_without_git_uses_the_active_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    _touch(repo / VARIANT, CARD, MODEL)
    monkeypatch.setenv("PATH", "")  # no git: nothing names a shared checkout
    assert _psp103_files(repo, None) == (repo / VARIANT / CARD, repo / VARIANT / MODEL)
    assert _psp103_files(tmp_path / "elsewhere", None) is None


@pytest.fixture
def psp103_request(tmp_path):
    if shutil.which("ngspice") is None:
        pytest.skip("requires ngspice")
    repo = Path(__file__).resolve().parents[1]
    files = _psp103_files(repo, os.environ.get("KLT_IHP_SG13G2_ROOT"))
    if files is None:
        pytest.skip("requires IHP cornerMOSlv.lib and compiled psp103.osdi")
    card, model = files

    (tmp_path / "body.spice").write_text(
        "* Nested PSP103 device\nVdrain d 0 0.6\nVgate g 0 0.7\n"
        "Xdut d g testdut\n.subckt testdut d g\n"
        "XM1 d g 0 0 sg13_lv_nmos W=1u L=0.13u\n.ends testdut\n"
    )
    return {
        "netlist": "body.spice",
        "models": {"lib": str(card)},
        "corners": {"process": ["mos_tt"], "temperature_c": [27]},
        "options": {"osdi_preload": [str(model)]},
    }


@pytest.mark.parametrize("analysis", ["op", "dc"])
def test_nested_osdi_current_matches_an_independent_meter(
    tmp_path, capsys, psp103_request, analysis
):
    path = "@n.xdut.xm1.nsg13_lv_nmos"
    request = psp103_request
    request["options"]["save_vectors"] = [path + "[ids]", path + "[ide]"]
    request["analysis"] = {
        "kind": analysis,
        "args": "" if analysis == "op" else "Vdrain 0.2 0.8 0.1",
    }
    if analysis == "op":
        measures = [
            {"name": "ids", "expr": path + "[ids]"},
            {"name": "ide", "expr": path + "[ide]"},
            {"name": "meter", "expr": "-i(vdrain)"},
        ]
    else:
        measures = [
            {"name": "peak", "expr": f"vecmax({path}[ids])"},
            {"name": "meter", "expr": "vecmax(-i(vdrain))"},
            {"name": "atbias", "spice": f".meas dc atbias FIND {path}[ids] AT=0.6"},
            {"name": "supply", "spice": ".meas dc supply FIND i(vdrain) AT=0.6"},
        ]
    request["measurements"] = [{**m, "unit": "A"} for m in measures]
    filename = tmp_path / "req.json"
    filename.write_text(json.dumps(request))
    assert main(["sim", str(filename), "--backend", "local", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    result = {m["name"]: m["value"] for m in report["corners"][0]["measurements"]}
    assert result["meter"] > 0
    if analysis == "op":
        assert result["ids"] == pytest.approx(result["meter"], rel=1e-4)
        assert result["ide"] == pytest.approx(result["meter"], rel=1e-4)
    else:
        assert result["peak"] == pytest.approx(result["meter"], rel=1e-4)
        assert result["atbias"] == pytest.approx(-result["supply"], rel=1e-4)
        assert 0 < result["atbias"] < result["peak"]
