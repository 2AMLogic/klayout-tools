"""``klt sim --report-out`` (issue #2975): file copy of the report envelope."""

from __future__ import annotations

import gzip
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import _read_samples


def _report(n=4):
    return {
        "schema_version": 1,
        "netlist": "tb.spice",
        "status": "pass",
        "environment": {"monte_carlo": {"n": n, "seed": 1000}},
        "measurements": [
            {"name": "vref", "unit": "V", "limits": {"min": 0.5, "max": 3.5}}
        ],
        "corners": [
            {
                "corner_id": f"tt/1.800V/27C/mc{i}",
                "status": "pass",
                "measurements": [{"name": "vref", "value": 1.0 + i, "unit": "V"}],
                "monte_carlo": {"sample_index": i, "seed": 1000 + i},
            }
            for i in range(n)
        ],
    }


@pytest.fixture
def fake_sim(monkeypatch):
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: _report())


def _run(tmp_path, capsys, *extra):
    request = tmp_path / "req.json"
    request.write_text("{}")
    code = main(["sim", str(request), "--format", "json", *extra])
    return code, capsys.readouterr()


def test_gz_report_is_byte_identical_to_stdout_and_deterministic(
    tmp_path, capsys, fake_sim
):
    a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    code, out = _run(tmp_path, capsys, "--report-out", str(a))
    assert code == 0
    assert gzip.decompress(a.read_bytes()).decode() == out.out
    _run(tmp_path, capsys, "--report-out", str(b))
    assert a.read_bytes() == b.read_bytes()


def test_plain_and_suffixless_paths_write_uncompressed_json(tmp_path, capsys, fake_sim):
    for name in ("r.json", "r"):
        path = tmp_path / name
        code, out = _run(tmp_path, capsys, "--report-out", str(path))
        assert code == 0
        assert path.read_text() == out.out


def test_unwritable_path_is_an_error_envelope(tmp_path, capsys, fake_sim):
    bad = tmp_path / "missing-dir" / "r.json.gz"
    code, out = _run(tmp_path, capsys, "--report-out", str(bad))
    assert code == 1
    assert out.out == ""
    assert json.loads(out.err)["error"]["command"] == "sim"


def test_yield_reads_gz_report_identically(tmp_path):
    from klayout_tools.cli.output import write_json_file

    plain, gz = tmp_path / "r.json", tmp_path / "r.json.gz"
    write_json_file(_report(), str(plain))
    write_json_file(_report(), str(gz))
    assert _read_samples(str(gz)) == _read_samples(str(plain))
