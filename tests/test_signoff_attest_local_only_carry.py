"""``klt signoff --attest N --inventory F -o OUT --skip-local-only`` (issue #5209)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from klayout_tools.cli import main
from klayout_tools.signoff_attest import build_attestation, render_envelope

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_BAD = [sys.executable, "-c", "import sys; sys.exit(1)"]
_PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb").mkdir()
    (repo / "attest").mkdir()
    (repo / "evidence").mkdir()
    (repo / "tb" / "a.spice").write_text("* a\n")
    return repo


def _inv(repo: Path, argv, local_only=True) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 9,
        "pins": _PINS,
        "testbenches": [
            {
                "path": "tb/a.spice",
                "cold_start": {"argv": argv, "local_only": local_only},
            }
        ],
    }
    path = repo / "attest" / "item9.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _attest(inventory, out, *extra):
    return main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            inventory,
            *extra,
            "--format",
            "json",
        ]
        + (["-o", str(out)] if out else [])
    )


def _seed(repo: Path, argv=_OK) -> tuple[str, Path]:
    inventory = _inv(repo, argv)
    out = repo / "evidence" / "item9.json"
    out.write_text(render_envelope(build_attestation(9, inventory)))
    return inventory, out


def test_skip_does_not_run_local_only_and_refreshes_hashes(tmp_path, capsys):
    repo = _repo(tmp_path)
    marker = repo / "ran.txt"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    inventory, out = _seed(repo, argv)
    marker.unlink()
    (repo / "tb" / "a.spice").write_text("* a changed\n")
    before = out.read_text()
    capsys.readouterr()

    assert _attest(inventory, out, "--skip-local-only") == 0

    assert not marker.exists()
    live = json.loads(capsys.readouterr().out)
    assert [c["role"] for c in live["skipped_checks"]] == ["cold_start"]
    written = out.read_text()
    assert written != before  # hash refreshed
    assert "skipped" not in written
    assert json.loads(written)["status"] == "pass"
    assert _attest(inventory, None, "--check", str(out)) == 0


def test_skip_carries_committed_failure_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    inventory, out = _seed(repo, _BAD)
    before = out.read_text()
    (repo / "tb" / "a.spice").write_text("* changed\n")

    assert _attest(inventory, out, "--skip-local-only") == 3
    assert out.read_text() == before


def test_skip_local_only_check_absent_from_out_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory, out = _seed(repo, _OK)
    inventory = _inv(repo, _BAD)  # different argv: no committed counterpart
    before = out.read_text()

    assert _attest(inventory, out, "--skip-local-only") == 3
    assert out.read_text() == before


def test_skip_text_output_prints_skip_line(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory, out = _seed(repo)
    capsys.readouterr()
    code = main(
        ["signoff", "--attest", "9", "--inventory", inventory, "-o", str(out)]
        + ["--skip-local-only"]
    )
    assert code == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_skip_refusals(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory, out = _seed(repo)
    missing = repo / "evidence" / "nope.json"
    garbage = repo / "evidence" / "bad.json"
    garbage.write_text("{not json")
    for extra, target in (
        (["--skip-local-only"], None),  # no -o
        (["--skip-local-only", "--check", str(out)], out),  # with --check (and -o)
        (["--skip-local-only"], missing),
        (["--skip-local-only"], garbage),
    ):
        capsys.readouterr()
        assert _attest(inventory, target, *extra) == 1
        assert "error" in json.loads(capsys.readouterr().err)
    capsys.readouterr()
    assert main(["signoff", "--skip-local-only", "--format", "json"]) == 1
    assert not missing.exists()


def test_skip_carries_across_inventory_metadata_only_edit(tmp_path, capsys):
    """A free-text-only inventory edit refreshes without rerunning (issue #5478).

    The reported trigger: editing nothing but the inventory's own
    ``description`` changes the envelope's pinned inventory hash, so the
    envelope needs reminting -- but no check, path or declared tool moved, so
    ``--skip-local-only`` must carry the committed ``cold_start`` outcome
    rather than re-running a command the inventory declared local-only.
    """
    repo = _repo(tmp_path)
    marker = repo / "ran.txt"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    inventory, out = _seed(repo, argv)
    marker.unlink()
    before = out.read_text()

    # Edit the inventory file itself, and only its free-text description.
    path = Path(inventory)
    loaded = json.loads(path.read_text())
    loaded["description"] = "item 9 cold-start attestation (wording touched up)"
    path.write_text(json.dumps(loaded))
    assert _attest(inventory, None, "--check", str(out)) == 3  # hash drift
    capsys.readouterr()

    assert _attest(inventory, out, "--skip-local-only") == 0

    assert not marker.exists()
    live = json.loads(capsys.readouterr().out)
    assert [c["role"] for c in live["skipped_checks"]] == ["cold_start"]
    written = out.read_text()
    assert written != before  # inventory hash re-pinned
    assert json.loads(written)["status"] == "pass"
    assert _attest(inventory, None, "--check", str(out)) == 0
