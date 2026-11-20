"""A committed functional-verification report can be checked without simulation."""

import hashlib
import io
import json
import sys

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.cli import main
from test_functional_verification import (
    _RESULTS_XML_WITH_SKIP,
    _base_request,
    _FakeRunner,
    _setup_inputs,
    _stub_runner,
    _write_request,
)
from test_functional_verification_verilog_top import _stage, needs_icarus


def _produce(request, capsys):
    assert main(["functional-verification", str(request), "--format", "json"]) == 0
    report = request.with_name("fv.json")
    report.write_text(capsys.readouterr().out)
    return report


def _forbid_simulation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("check mode reached simulator or tool setup")

    for name in (
        "_import_runner",
        "_engine_version",
        "_run_verilog_top",
        "_check_required_tool",
    ):
        monkeypatch.setattr(fv, name, forbidden)


def _check(request, report, capsys, expected_code, *extra):
    rc = main(
        [
            "functional-verification",
            str(request),
            "--check",
            str(report),
            "--format",
            "json",
            *map(str, extra),
        ]
    )
    output = capsys.readouterr()
    assert rc == expected_code, output
    if rc == 1:
        return json.loads(output.err)["error"]["message"]
    result = json.loads(output.out)
    assert (result["schema_version"], result["mode"], result["report"]) == (
        1,
        "check",
        str(report),
    )
    assert result["status"] == ("match" if rc == 0 else "drifted")
    return {c["field"]: c for c in result["checks"] if not c["match"]}


@pytest.fixture
def rtl(tmp_path, monkeypatch, capsys):
    _setup_inputs(tmp_path)
    (tmp_path / "util.v").write_text("module util; endmodule\n")
    request = tmp_path / "request.json"
    doc = _base_request(sources=["util.v", "gcd.v"], requires_tools=[{"name": "git"}])
    (tmp_path / "inc").mkdir()
    doc["options"]["includes"] = ["inc"]
    _write_request(request, doc)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    report = _produce(request, capsys)
    _forbid_simulation(monkeypatch)
    return request, report


def test_rtl_round_trip_and_drift(rtl, tmp_path, capsys):
    request, report = rtl
    assert _check(request, report, capsys, 0, "--outdir", tmp_path / "unused") == {}
    assert not (tmp_path / "unused").exists()
    original = json.loads(report.read_text())
    source = tmp_path / "util.v"
    source.write_bytes(b"// new RTL")
    drift = _check(request, report, capsys, 3)
    assert set(drift) == {
        "environment.sources[0].sha256",
        "provenance.input.content_hash",
    }
    change = drift["environment.sources[0].sha256"]
    assert change["expected"] == original["environment"]["sources"][0]["sha256"]
    assert change["actual"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert main(["functional-verification", str(request), "--check", str(report)]) == 3
    text = capsys.readouterr().out
    assert "[DRIFTED] environment.sources[0].sha256" in text
    assert change["expected"] in text and change["actual"] in text


def test_request_forms_relocation_and_option_drift(rtl, tmp_path, monkeypatch, capsys):
    request, report = rtl
    doc = json.loads(request.read_text())
    base = tmp_path / "checkout"
    base.mkdir()
    for name in doc["sources"]:
        (tmp_path / name).rename(base / name)
    (tmp_path / "test_gcd.py").unlink()  # unpinned and unnecessary for a check
    reordered = json.dumps(doc, sort_keys=True, indent=3)
    monkeypatch.setattr(sys, "stdin", io.StringIO(reordered))
    for arg in (request, "-", reordered):
        assert _check(arg, report, capsys, 0, "--base-dir", base) == {}
    doc["options"]["random_seed"] = 7
    assert set(_check(json.dumps(doc), report, capsys, 3, "--base-dir", base)) == {
        "provenance.request_sha256"
    }


def test_reordered_sources_are_reported_even_with_same_aggregate(rtl, capsys):
    request, report = rtl
    doc = json.loads(request.read_text())
    doc["sources"].reverse()
    _write_request(request, doc)
    drift = _check(request, report, capsys, 3)
    assert set(drift) == {
        "provenance.request_sha256",
        "environment.sources[0].sha256",
        "environment.sources[1].sha256",
    }


@pytest.mark.parametrize("refresh_synthesis", [True, False])
def test_synthesized_netlist_compared_to_fv_pin(
    tmp_path, monkeypatch, capsys, refresh_synthesis
):
    _setup_inputs(tmp_path)
    netlist = tmp_path / "gcd.v"
    pinned = hashlib.sha256(netlist.read_bytes()).hexdigest()
    synth = {
        "hdl_toplevel": "gcd",
        "netlist_path": {"scope": "external", "path": None},
        "netlist_sha256": pinned,
    }
    synthesis_report = tmp_path / "synth.json"
    synthesis_report.write_text(json.dumps(synth))
    request = tmp_path / "request.json"
    _write_request(
        request,
        _base_request(
            sources=[],
            options={"from_synthesize": {"netlist": "gcd.v", "report": "synth.json"}},
        ),
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    report = _produce(request, capsys)
    _forbid_simulation(monkeypatch)
    assert _check(request, report, capsys, 0) == {}
    netlist.write_bytes(b"// refreshed")
    actual = hashlib.sha256(netlist.read_bytes()).hexdigest()
    if refresh_synthesis:
        synth["netlist_sha256"] = actual
        synthesis_report.write_text(json.dumps(synth))
    drift = _check(request, report, capsys, 3)
    assert set(drift) == {
        "from_synthesize.netlist_sha256",
        "environment.sources[0].sha256",
    }
    assert drift["from_synthesize.netlist_sha256"]["expected"] == pinned
    assert drift["from_synthesize.netlist_sha256"]["actual"] == actual
    legacy = json.loads(report.read_text())
    del legacy["from_synthesize"]["netlist_sha256"]
    report.write_text(json.dumps(legacy))
    assert "from_synthesize.netlist_sha256" in _check(request, report, capsys, 1)


@needs_icarus
def test_verilog_top_sdf_uses_original_bytes_in_current_checkout(
    tmp_path, capsys, monkeypatch
):
    design = tmp_path / "original"
    design.mkdir()
    request = _stage(design, "request-sdf.json")
    report = _produce(request, capsys)
    _forbid_simulation(monkeypatch)
    design.rename(tmp_path / "relocated")
    design = tmp_path / "relocated"
    request, report = design / request.name, design / report.name
    assert _check(request, report, capsys, 0) == {}
    sdf = design / "and2.sdf"
    pinned = hashlib.sha256(sdf.read_bytes()).hexdigest()
    sdf.write_bytes(sdf.read_bytes() + b"\n")
    (change,) = _check(request, report, capsys, 3).values()
    assert (change["field"], change["expected"], change["actual"]) == (
        "provenance.sdf_sha256",
        pinned,
        hashlib.sha256(sdf.read_bytes()).hexdigest(),
    )
    legacy = json.loads(report.read_text())
    del legacy["provenance"]["sdf_sha256"]
    report.write_text(json.dumps(legacy))
    assert "sdf_sha256" in _check(request, report, capsys, 1)


@pytest.mark.parametrize(
    "field", ["request_sha256", "content_hash", "sources", "sha256"]
)
def test_incomplete_or_malformed_pins_are_errors(rtl, capsys, field):
    request, report = rtl
    data = json.loads(report.read_text())
    if field == "request_sha256":
        del data["provenance"][field]
    elif field == "content_hash":
        data["provenance"]["input"][field] = "junk"
    elif field == "sources":
        data["environment"][field] = []
    else:
        del data["environment"]["sources"][1][field]
    report.write_text(json.dumps(data))
    assert field in _check(request, report, capsys, 1)


def test_malformed_documents_are_errors(rtl, capsys):
    request, report = rtl
    request.write_text("[]")
    assert "object" in _check(request, report, capsys, 1)
    _write_request(request, _base_request())
    report.write_text("{ malformed")
    assert "JSON" in _check(request, report, capsys, 1)


@pytest.mark.parametrize(
    "override,field",
    [
        ({"options": {"random_seed": "bad"}}, "random_seed"),
        ({"testbench": {}}, "testbench.module"),
        ({"requires_tools": ["iverilog"]}, "requires_tools"),
    ],
)
def test_invalid_request_fields_are_errors(rtl, capsys, override, field):
    request, report = rtl
    doc = json.loads(request.read_text())
    _write_request(request, {**doc, **override})
    assert field in _check(request, report, capsys, 1)
