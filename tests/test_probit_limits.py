"""Campaign-level limits use the profile interval at the requested confidence."""

import json

import pytest

from klayout_tools import probit
from klayout_tools.cli import main


def records():
    return [
        {"stimulus": stimulus, "outcome": index < count}
        for stimulus, count in [(-1, 20), (0, 50), (1, 80)]
        for index in range(100)
    ]


def margin(separation, multiple=6, source="campaign/worst-separation"):
    return {"separation": separation, "multiple": multiple, "source": source}


def invoke(tmp_path, capsys, doc, *args, fmt="json"):
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(doc))
    code = main(["yield-threshold", str(path), "--format", fmt, *args])
    result = capsys.readouterr()
    if code == 1:
        assert not result.out
        return code, json.loads(result.err)["error"]
    return code, json.loads(result.out) if fmt == "json" else result.out


@pytest.mark.parametrize(
    ("maximum", "separation", "verdict", "code"),
    [(1.3, 8, "fail", 3), (1.6, 10, "pass", 0)],
)
def test_both_checks_and_formats(tmp_path, capsys, maximum, separation, verdict, code):
    limits = {"sigma_max": maximum, "margin": margin(separation)}
    doc = {"records": records(), "limits": limits}
    actual_code, report = invoke(tmp_path, capsys, doc)
    assert actual_code == code
    corner = report["corners"][0]
    assert report["status"] == corner["verdict"] == verdict
    bound = corner["fit"]["sigma_ci"]["upper"]
    assert bound == pytest.approx(1.547361, abs=1e-6)
    checks = corner["checks"]
    assert checks["sigma_max"] == {
        "maximum": maximum,
        "sigma_ci_upper": bound,
        "verdict": verdict,
    }
    assert checks["margin"] == {
        **margin(separation),
        "sigma_ci_upper": bound,
        "verdict": verdict,
    }
    text_code, text = invoke(tmp_path, capsys, doc, fmt="text")
    assert text_code == code
    for field in (
        "sigma_max",
        "margin",
        verdict,
        "campaign/worst-separation",
        "1.54736",
    ):
        assert field in text


def test_inclusive_comparison_and_requested_confidence(tmp_path, capsys):
    doc = {"corners": [{"name": "tt", "records": records()}]}
    _, raw = invoke(tmp_path, capsys, doc)
    upper = raw["corners"][0]["fit"]["sigma_ci"]["upper"]
    assert "status" not in raw and "checks" not in raw["corners"][0]
    doc["limits"] = {"sigma_max": upper, "margin": margin(6 * upper)}
    assert invoke(tmp_path, capsys, doc)[0] == 0
    assert invoke(tmp_path, capsys, doc, "--confidence", "0.99")[0] == 3
    doc["limits"] = {"sigma_max": upper - 1e-12}
    assert invoke(tmp_path, capsys, doc)[0] == 3


def test_failed_corner_precedes_insufficient_evidence(tmp_path, capsys):
    empty = {"name": "empty", "records": []}
    doc = {
        "limits": {"sigma_max": 1.3},
        "corners": [empty, {"name": "tt", "records": records()}],
    }
    code, report = invoke(tmp_path, capsys, doc)
    assert code == 3 and report["status"] == "fail"
    check = report["corners"][0]["checks"]["sigma_max"]
    assert (
        check["verdict"] == "insufficient_evidence" and check["sigma_ci_upper"] is None
    )
    assert "no usable samples" in check["reason"]
    doc["corners"] = [empty]
    assert invoke(tmp_path, capsys, doc)[0] == 4
    del doc["limits"]
    assert invoke(tmp_path, capsys, doc)[0] == 0
    assert invoke(tmp_path, capsys, {"records": [], "limits": {"sigma_max": 1}})[0] == 4


def test_open_upper_bound(tmp_path, capsys, monkeypatch):
    original = probit.fit_probit

    def open_upper(*args):
        fit = original(*args)
        fit["sigma_ci"]["upper"] = None
        return fit

    monkeypatch.setattr(probit, "fit_probit", open_upper)
    doc = {"records": records(), "limits": {"margin": margin(8)}}
    code, report = invoke(tmp_path, capsys, doc)
    assert code == 4 and report["status"] == "insufficient_evidence"
    assert "upper" in report["corners"][0]["checks"]["margin"]["reason"]
    assert "upper" in invoke(tmp_path, capsys, doc, fmt="text")[1]


@pytest.mark.parametrize(
    "limits",
    [
        None,
        {},
        {"extra": 1},
        [],
        {"sigma_max": False},
        {"sigma_max": float("inf")},
        {"sigma_max": -1},
        {"margin": "bad"},
        {"margin": {}},
        {"margin": margin(-1)},
        {"margin": margin(1, 0)},
        {"margin": margin(1, True)},
        {"margin": margin(1, source=" ")},
        {"margin": {**margin(1), "unknown": 1}},
        {"margin": margin(float("nan"))},
    ],
)
def test_invalid_declarations_use_input_error(tmp_path, capsys, limits):
    code, error = invoke(tmp_path, capsys, {"records": [], "limits": limits})
    assert code == 1 and "limits" in str(error)


def test_invalid_json_reports_a_document_error(tmp_path, capsys):
    path = tmp_path / "records.json"
    path.write_text("{")
    assert main(["yield-threshold", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert (
        not captured.out
        and "not valid JSON" in json.loads(captured.err)["error"]["message"]
    )
