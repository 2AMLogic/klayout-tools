"""Pair nominal and control CSV draws and pass the resulting document to yield."""

import hashlib
import importlib.util
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import derive_sample_set_from_csv


def _args(tmp_path):
    nominal = tmp_path / "nominal.csv"
    nominal.write_text(
        "metric,result,status,id\nrail,1,ok,1\nrail,1.02,ok,2\nnoise,0.98,ok,1\nnoise,1,ok,2\n"
    )
    control = tmp_path / "fault.csv"
    control.write_bytes(
        (
            "\ufeffmetric,result,status,id\r\n"
            "noise,0.1,ok,0\r\nrail,0.2,ok,1\r\nrail,0.1,ok,2\r\n"
            "noise,0.2,ok,2\r\nrail,,bad,3\r\nrail,no,ok,4\r\n"
            "noise,NaN,ok,3\r\nnoise,,ok,4\r\nextra,42,ok,1\r\n"
        ).encode()
    )
    args = [
        "yield-csv",
        str(nominal),
        "--group-by",
        "metric",
        "--value",
        "result",
        "--status-col",
        "status",
        "--pass-value",
        "ok",
        "--draw-col",
        "id",
        "--min",
        "0.9",
        "--format",
        "json",
    ]
    return nominal, control, args


def test_control_and_provenance_document(tmp_path, capsys):
    _, control, args = _args(tmp_path)
    assert (
        main(
            [
                *args,
                "--negative-control",
                str(control),
                "--description",
                "injected open",
                "--seed",
                "0",
                "--rng",
                "PCG64",
                "--draws-per-population",
                "4",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["provenance"] == {"seed": 0, "rng": "PCG64", "draws_per_population": 4}
    expected = {"rail": [0.2, 0.1], "noise": [0.1, 0.2]}
    assert len(doc["measurements"]) == 2
    for m in doc["measurements"]:
        assert m["negative_control"] == {
            "samples": expected[m["name"]],
            "errored": 2,
            "failed_unmeasurable": 0,
            "censored": 0,
            "description": "injected open",
        }
    assert doc["derivation"]["negative_control"] == {
        "path": str(control),
        "content_hash": "sha256:" + hashlib.sha256(control.read_bytes()).hexdigest(),
        "rows": 9,
        "description": "injected open",
    }


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("metric,result,status,id\nnoise,1,ok,1\n", "rail"),
        ("metric,other,status,id\nrail,0,ok,1\n", "result"),
        ("metric,result,status,id\nrail,1,ok,1\nnoise,,ok,1\n", "no usable draws"),
        ("metric,result,status,id\nrail,1,ok,1\nrail,2,ok,1\n", "1"),
    ],
)
def test_control_errors_name_the_file(tmp_path, capsys, content, fragment):
    _, control, args = _args(tmp_path)
    control.write_text(content)
    assert main([*args, "--negative-control", str(control)]) == 1
    output = capsys.readouterr()
    assert not output.out
    message = json.loads(output.err)["error"]["message"]
    assert str(control) in message and fragment in message


def test_legacy_and_separately_optional_flags(tmp_path, capsys):
    nominal, _, args = _args(tmp_path)
    assert main(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == derive_sample_set_from_csv(
        str(nominal),
        group_by="metric",
        value="result",
        status_col="status",
        pass_value="ok",
        draw_col="id",
        limits={"min": 0.9},
    )
    assert main([*args, "--rng", "Philox"]) == 0
    with_rng = json.loads(capsys.readouterr().out)
    assert with_rng.pop("provenance") == {"rng": "Philox"}
    assert with_rng == doc
    assert main([*args, "--negative-control", str(nominal)]) == 0
    paired = json.loads(capsys.readouterr().out)
    for m in paired["measurements"]:
        assert m["negative_control"]["samples"] == m["samples"]
    for flag, value, reason in [
        ("--draws-per-population", "0", "positive"),
        ("--description", "orphan", "--negative-control"),
    ]:
        assert main([*args, flag, value]) == 1
        output = capsys.readouterr()
        assert not output.out and reason in json.loads(output.err)["error"]["message"]


@pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason="needs the yield extension",
)
def test_derived_document_grades_bad_control(tmp_path, capsys):
    _, control, args = _args(tmp_path)
    assert main([*args, "--negative-control", str(control), "--seed", "42"]) == 0
    sample = tmp_path / "samples.json"
    sample.write_text(capsys.readouterr().out)
    code = main(["yield", str(sample), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert code in (0, 3)
    assert report["source"]["provenance"] == {"seed": 42}
    assert all(m["negative_control"] is not None for m in report["measurements"])
