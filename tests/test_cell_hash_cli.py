"""The cell-hash command emits a digest suitable for a foundry-cell exception."""

import json
import re

import klayout.db as kdb
import pytest

from klayout_tools._provenance import cell_geometry_digest
from klayout_tools.cli import main
from test_signoff_foundry_cell_exception import _gds, _item3, _setup


@pytest.mark.parametrize("extension", ["gds", "oas"])
def test_hash_matches_bare_and_embedded_cell(tmp_path, capsys, extension):
    bare = _gds(tmp_path / f"bare.{extension}")
    block = _gds(tmp_path / f"block.{extension}", with_top=True)
    expected = cell_geometry_digest(bare, "PAD")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", expected)
    assert main(["cell-hash", "--cell", "PAD", bare]) == 0
    assert capsys.readouterr().out == expected + "\n"
    assert main(["cell-hash", "--cell", "PAD", block, "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cell_hash"] == expected
    assert result["schema_version"] == 1


def test_hash_is_accepted_by_signoff(tmp_path, capsys):
    report, exception = _setup(tmp_path)
    assert main(["cell-hash", "--cell", "PAD", exception["bare_cell_layout"]]) == 0
    exception["cell_hash"] = capsys.readouterr().out.strip()
    assert _item3(report, [exception])["status"] == "met"


def test_child_geometry_changes_hash(tmp_path, capsys):
    path = tmp_path / "hierarchy.gds"
    _gds(path, with_top=True)
    argv = ["cell-hash", "--cell", "TOP", str(path)]
    assert main(argv) == 0
    before = capsys.readouterr().out
    layout = kdb.Layout()
    layout.read(str(path))
    layout.cell("PAD").shapes(layout.layer(71, 20)).insert(kdb.Box(3000, 0, 4000, 1000))
    layout.write(str(path))
    assert main(argv) == 0
    assert capsys.readouterr().out != before


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("missing_cell", [True, False])
def test_hash_failure_names_cell_and_layout(tmp_path, capsys, missing_cell, fmt):
    path = tmp_path / "design.gds"
    if missing_cell:
        _gds(path)
    else:
        path.write_bytes(b"not a layout")
    assert main(["cell-hash", "--cell", "ABSENT", str(path), "--format", fmt]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    message = json.loads(err)["error"]["message"] if fmt == "json" else err
    assert "ABSENT" in message and str(path) in message


@pytest.mark.parametrize("args", [["design.gds"], ["--cell", "PAD"]])
def test_missing_arguments_are_usage_errors(args):
    with pytest.raises(SystemExit) as usage:
        main(["cell-hash", *args])
    assert usage.value.code == 2
