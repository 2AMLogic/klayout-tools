"""External critical metrics fail closed without forbidding signed measurements."""

import io
import json
import subprocess

import pytest

from klayout_tools import metrics, signoff
from klayout_tools.cli import main

CRITICAL_COUNTS = (
    "drc__error__count",
    "sim__corner__failed_count",
    "sim__corner__errored_count",
)
INVALID_COUNTS = [
    pytest.param(-1, "below_minimum", id="negative"),
    pytest.param("bad", "expected_integer", id="string"),
    pytest.param(None, "expected_integer", id="null"),
    pytest.param(True, "expected_integer", id="true"),
    pytest.param(False, "expected_integer", id="false"),
    pytest.param(0.5, "expected_integer", id="fraction"),
    pytest.param(0.0, "expected_integer", id="float-zero"),
    pytest.param([], "expected_integer", id="array"),
    pytest.param({}, "expected_integer", id="object"),
]
NONFINITE_TOKENS = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]


def envelope(name="drc__error__count", value=0):
    return {
        "schema_version": 1,
        "status": "clean",
        "violations": [],
        "metrics": {name: value},
    }


def write_json(tmp_path, payload, name="evidence.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


@pytest.mark.parametrize("name", CRITICAL_COUNTS)
@pytest.mark.parametrize("value,reason", INVALID_COUNTS)
@pytest.mark.parametrize("source_kind", ["file", "stdin"])
def test_malformed_critical_count_fails_with_metric_reason(
    tmp_path, monkeypatch, name, value, reason, source_kind
):
    payload = envelope(name, value)
    source = write_json(tmp_path, payload)
    if source_kind == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        source = "-"
    result = signoff.build_signoff([source])
    assert result["status"] == "fail"
    blocker = result["checks"][0]["detail"]["critical_metric_blockers"][0]
    assert blocker["metric"] == name
    assert blocker["reason"] == reason
    assert blocker["domain"] == "nonnegative_integer"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value,reason", INVALID_COUNTS)
def test_manifest_invalid_metric_has_structured_failure(tmp_path, value, reason):
    source = write_json(tmp_path, envelope(value=value))
    result = signoff.build_tier_report({"kind": "analog", "evidence": {"3": source}})
    item = next(item for item in result["items"] if item["id"] == 3)
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["citation"] is None
    assert item["detail"]["critical_metric_blockers"][0]["reason"] == reason
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("name", CRITICAL_COUNTS)
@pytest.mark.parametrize(
    "value,expected", [(0, "pass"), (1, "fail"), (2**1000, "fail")]
)
def test_critical_count_boundaries(tmp_path, name, value, expected):
    assert (
        signoff.build_signoff([write_json(tmp_path, envelope(name, value))])["status"]
        == expected
    )


def test_legacy_and_unknown_optional_metrics_keep_their_behavior(tmp_path):
    payload = envelope("optional__unregistered", "unknown")
    assert signoff.build_signoff([write_json(tmp_path, payload)])["status"] == "pass"
    del payload["metrics"]
    assert signoff.build_signoff([write_json(tmp_path, payload)])["status"] == "pass"


def test_signed_domain_preserves_polarity_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "REGISTRY", dict(metrics.REGISTRY))
    metrics.register(
        "test__signed__measurement",
        aggregator="min",
        higher_is_better=False,
        critical=True,
        domain="finite_number",
    )
    assert (
        signoff.build_signoff(
            [write_json(tmp_path, envelope("test__signed__measurement", -0.125))]
        )["status"]
        == "pass"
    )
    metrics.register(
        "test__timing__slack",
        aggregator="min",
        higher_is_better=True,
        critical=True,
        domain="finite_number",
    )
    result = signoff.build_signoff(
        [write_json(tmp_path, envelope("test__timing__slack", -0.125))]
    )
    blocker = result["checks"][0]["detail"]["critical_metric_blockers"][0]
    assert "domain" not in blocker  # valid signed value fails its quality threshold


@pytest.mark.parametrize("token", NONFINITE_TOKENS)
@pytest.mark.parametrize("mode", ["envelope", "manifest", "fleet"])
@pytest.mark.parametrize("source_kind", ["file", "stdin"])
def test_cli_rejects_nonfinite_json_at_every_source(
    tmp_path, monkeypatch, capsys, token, mode, source_kind
):
    payload = {
        "envelope": envelope(),
        "manifest": {"kind": "analog"},
        "fleet": {"blocks": [{"block": "demo", "kind": "analog"}]},
    }[mode]
    # Even an otherwise unused field is rejected before it can be echoed later.
    text = json.dumps(payload)[:-1] + ', "external": ' + token + "}"
    path = tmp_path / "input.json"
    path.write_text(text)
    source = str(path)
    if source_kind == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        source = "-"
    args = [source] if mode == "envelope" else ["--" + mode, source]
    assert main(["signoff", *args, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err
    assert captured.out == ""
    json.dumps(json.loads(captured.err), allow_nan=False)


@pytest.mark.parametrize("token", NONFINITE_TOKENS)
def test_manifest_evidence_nonfinite_is_unreadable(tmp_path, token):
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(envelope()).replace(
            '"drc__error__count": 0', '"drc__error__count": ' + token
        )
    )
    result = signoff.build_tier_report({"kind": "analog", "evidence": {"3": str(path)}})
    item = next(item for item in result["items"] if item["id"] == 3)
    assert (item["status"], item["reason"]) == ("unmet", "unreadable_evidence")
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("token", NONFINITE_TOKENS)
def test_command_evidence_nonfinite_is_unreadable(monkeypatch, token):
    text = json.dumps(envelope()).replace(
        '"drc__error__count": 0', '"drc__error__count": ' + token
    )
    monkeypatch.setattr(
        signoff.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, text, ""),
    )
    result = signoff.build_tier_report(
        {"kind": "analog", "evidence": {"3": {"command": ["synthetic-check"]}}}
    )
    item = next(item for item in result["items"] if item["id"] == 3)
    assert (item["status"], item["reason"]) == ("unmet", "unreadable_evidence")
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("token", NONFINITE_TOKENS)
def test_nested_fleet_manifest_rejects_nonfinite_json(tmp_path, token):
    path = tmp_path / "block.json"
    path.write_text('{"block": "demo", "kind": "analog", "external": ' + token + "}")
    with pytest.raises(signoff.SignoffError, match="not valid JSON"):
        signoff.build_fleet_report({"blocks": [str(path)]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_critical_values_have_serializable_diagnostics(value):
    blockers = signoff._critical_metric_blockers(envelope(value=value))
    assert blockers[0]["metric"] == "drc__error__count"
    assert blockers[0]["reason"] == "non_finite"
    json.dumps(blockers, allow_nan=False)


@pytest.mark.parametrize("value", ["bad", None, True, []])
def test_default_finite_domain_rejects_nonnumeric_critical_values(
    tmp_path, monkeypatch, value
):
    monkeypatch.setattr(metrics, "REGISTRY", dict(metrics.REGISTRY))
    metrics.register(
        "test__finite__value", aggregator="min", higher_is_better=True, critical=True
    )
    result = signoff.build_signoff(
        [write_json(tmp_path, envelope("test__finite__value", value))]
    )
    assert result["status"] == "fail"
    blocker = result["checks"][0]["detail"]["critical_metric_blockers"][0]
    assert (blocker["domain"], blocker["reason"]) == (
        "finite_number",
        "expected_number",
    )


def test_command_evidence_explains_invalid_count(monkeypatch):
    text = json.dumps(envelope(value=-1))
    monkeypatch.setattr(
        signoff.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, text, ""),
    )
    result = signoff.build_tier_report(
        {"kind": "analog", "evidence": {"3": {"command": ["synthetic-check"]}}}
    )
    item = next(item for item in result["items"] if item["id"] == 3)
    assert item["citation"] is None
    assert item["detail"]["critical_metric_blockers"][0]["reason"] == "below_minimum"


@pytest.mark.parametrize("source_kind", ["file", "stdin"])
def test_cli_manifest_explains_invalid_count(
    tmp_path, monkeypatch, capsys, source_kind
):
    source = write_json(tmp_path, envelope(value=True))
    manifest = {"kind": "analog", "evidence": {"3": source}}
    path = write_json(tmp_path, manifest, "manifest.json")
    if source_kind == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(manifest)))
        path = "-"
    assert main(["signoff", "--manifest", path, "--format", "json"]) == 3
    result = json.loads(capsys.readouterr().out)
    item = next(item for item in result["items"] if item["id"] == 3)
    assert item["detail"]["critical_metric_blockers"][0]["reason"] == "expected_integer"
    json.dumps(result, allow_nan=False)
