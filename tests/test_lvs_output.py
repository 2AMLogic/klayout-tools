"""Issue #4984: ``klt lvs -o/--output`` and the ``{path, scope}`` ``reference``."""

import gzip
import json
import os

import pytest

from klayout_tools.cli import main
from klayout_tools.lvs import check_lvs_report, run_lvs

_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


@pytest.fixture
def tmp_path(tmp_path):
    """Seed a `.git` marker so tmp_path is the repo root (`scope: "repo"`),
    as in tests/test_lvs.py."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write(path, text):
    path.write_text(text)
    return str(path)


def _pair(tmp_path):
    pair = tmp_path / "pair"
    pair.mkdir()
    _write(pair / "layout.spice", _SPICE)
    _write(pair / "ref.spice", _SPICE)
    request = pair / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": "layout.spice", "top": "inv"},
                "reference": {"netlist": "ref.spice", "top": "inv"},
            }
        )
    )
    report = pair / "lvs.json"
    report.write_text(json.dumps(run_lvs(str(request))))
    return pair, report


def test_check_lvs_report_reference_resolves_from_any_cwd(tmp_path, monkeypatch):
    """Issue #4984: a v3 report's `reference` is `{path, scope}`, so
    `--check` from an unrelated cwd matches (no `input_not_found`)."""
    _pair(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = check_lvs_report(os.path.join("pair", "lvs.json"))

    assert result["status"] == "match"
    assert not any("input_not_found" in check for check in result["checks"])


def test_lvs_output_flag_writes_envelope_and_rejects_check(tmp_path, capsys):
    """Issue #4984: `-o` mirrors stdout JSON; incompatible with --check;
    unwritable path is an error envelope."""
    layout_path, reference_path = (
        _write(tmp_path / "layout.spice", _SPICE),
        _write(tmp_path / "ref.spice", _SPICE),
    )
    request = json.dumps(
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        }
    )
    out = tmp_path / "report.json"
    assert main(["lvs", request, "-o", str(out), "--format", "json"]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(out.read_text()) == json.loads(stdout)

    gz = tmp_path / "report.json.gz"
    assert main(["lvs", request, "-o", str(gz), "--format", "json"]) == 0
    capsys.readouterr()
    assert json.loads(gzip.open(gz, "rt").read())["status"] == "match"

    assert main(["lvs", "--check", str(out), "-o", str(gz), "--format", "json"]) == 1
    capsys.readouterr()

    bad = tmp_path / "no_such_dir" / "r.json"
    assert main(["lvs", request, "-o", str(bad), "--format", "json"]) == 1
    capsys.readouterr()
