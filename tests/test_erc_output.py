"""ERC saves the existing report envelope alongside the requested stdout format."""

import gzip
import json

import pytest

from klayout_tools.cli import main
from test_erc import _antenna_fixture, _basic_spec, _full_stack_spec


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_saved_erc_report_matches_stdout_and_verifies(
    tmp_path, monkeypatch, capsys, suffix
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    _antenna_fixture(tmp_path / "cell.gds", li1_um2=0.2, met1_um2=1.0, met2_um2=1.0)
    _basic_spec(tmp_path / "spec.json")
    output = tmp_path / ("report" + suffix)
    args = ["erc", "cell.gds", "spec.json", "--pdk", "sky130", "--format", "json"]
    assert main([*args, "-o", str(output)]) == 0
    stdout = capsys.readouterr().out
    saved = output.read_bytes()
    if suffix.endswith(".gz"):
        saved = gzip.decompress(saved)
    assert saved == stdout.encode()
    assert saved.endswith(b"\n")
    data = json.loads(saved)
    assert data["file"] == {"path": "cell.gds", "scope": "repo"}
    assert data["spec"] == "spec.json"
    assert main(["erc", "--check", str(output), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


@pytest.mark.parametrize(
    ("with_pdk", "full_stack", "li1_area", "status", "exit_code"),
    [
        (True, True, 0.2, "clean_partial", 0),
        (True, False, 80.0, "violations", 3),
        (False, False, 0.2, "not_checked", 4),
    ],
)
def test_text_run_preserves_completed_verdict(
    tmp_path, capsys, with_pdk, full_stack, li1_area, status, exit_code
):
    layout, spec, output = (
        tmp_path / name for name in ("cell.gds", "spec.json", "result")
    )
    _antenna_fixture(layout, li1_um2=li1_area, met1_um2=1.0, met2_um2=1.0)
    (_full_stack_spec if full_stack else _basic_spec)(spec)
    args = ["erc", str(layout), str(spec), "--output", str(output)]
    if with_pdk:
        args.extend(["--pdk", "sky130"])
    assert main(args) == exit_code
    assert status in capsys.readouterr().out
    assert json.loads(output.read_bytes())["status"] == status


@pytest.mark.parametrize(
    "mode",
    [
        ["--check", "report"],
        ["--check", "report", "--rerun"],
        ["--validate-spec", "spec.json"],
        ["--pnr-response", "run.json"],
        ["missing.gds", "missing.json"],
    ],
)
def test_error_preserves_existing_erc_report(tmp_path, capsys, mode):
    output = tmp_path / "previous.json"
    output.write_bytes(b"previous report")
    assert main(["erc", *mode, "-o", str(output), "--format", "json"]) == 1
    streams = capsys.readouterr()
    assert streams.out == ""
    assert json.loads(streams.err)["error"]["command"] == "erc"
    if mode[0].startswith("--"):
        assert "--output" in streams.err
    assert output.read_bytes() == b"previous report"


def test_write_error_names_destination(tmp_path, capsys):
    layout, spec = tmp_path / "cell.gds", tmp_path / "spec.json"
    _antenna_fixture(layout, li1_um2=0.2)
    _basic_spec(spec)
    dest = tmp_path / "missing-parent" / "result"
    assert (
        main(["erc", str(layout), str(spec), "-o", str(dest), "--format", "json"]) == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert str(dest) in json.loads(output.err)["error"]["message"]
