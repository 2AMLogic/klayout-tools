"""Request-level ABC mapping choices and the evidence that records them."""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from klayout_tools import synthesize, synthesize_abc
from klayout_tools.cli import main
from test_signoff import _synth_item_1, _synthesize_envelope
from test_synthesize import (
    _REAL_SKY130_VARIANT,
    HAVE_YOSYS,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _setup(tmp_path, monkeypatch, mapping):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = Path(_setup_success_env(tmp_path, monkeypatch))
    request = json.loads(path.read_text())
    request["synthesis"] = mapping
    path.write_text(json.dumps(request))
    _stub_yosys_success(monkeypatch)
    return path


@pytest.mark.parametrize(
    ("mapping", "field"),
    [
        ([], "synthesis"),
        ({"abc_fast": 1}, "abc_fast"),
        ({"abc_script": "missing.abc"}, "abc_script"),
        ({"abc_script": "."}, "abc_script"),
        ({"abc_script": ""}, "abc_script"),
        ({"abc_fast": True, "abc_script": "abc"}, "abc_script"),
        ({"typo": True}, "typo"),
    ],
)
def test_invalid_mapping_is_a_request_error(tmp_path, capsys, mapping, field):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_base_request(synthesis=mapping)))
    assert main(["synthesize", str(path), "--format", "json"]) == 1
    assert field in json.loads(capsys.readouterr().err)["error"]["message"]


def test_fast_replaces_both_mapping_passes(tmp_path, monkeypatch):
    path = _setup(tmp_path, monkeypatch, {"abc_fast": True})
    monkeypatch.setattr(synthesize, "_abc_supports_option", lambda _: True)
    report = synthesize.run_synthesize(str(path))
    script = (tmp_path / report["script_path"]["path"]).read_text()
    assert "synth -noabc -top gcd\nabc -fast\nopt -fast\n" in script
    assert "abc -fast -liberty" in script
    assert report["synthesis"] == {
        "abc_fast": True,
        "abc_script": None,
        "reduced_passes": ["synth_internal", "liberty"],
    }


def test_custom_script_hash_and_replay(tmp_path, monkeypatch):
    path = _setup(tmp_path, monkeypatch, {"abc_script": "+custom map.abc"})
    script_path = tmp_path / "+custom map.abc"
    script_path.write_bytes(b"strash\nmap\n")
    report = synthesize.run_synthesize(str(path))
    script = (tmp_path / report["script_path"]["path"]).read_text()
    assert "synth -top gcd\n" in script and "abc -fast" not in script
    assert " -script " in script
    recorded = report["synthesis"]
    assert recorded["abc_script"] == {
        "path": script_path.name,
        "sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
    }
    assert recorded["reduced_passes"] == ["liberty"]
    assert report["provenance"]["abc_script"] == recorded["abc_script"]
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    assert (
        synthesize.check_synthesize_report(str(committed), str(path))["status"]
        == "match"
    )
    assert (
        synthesize.rerun_synthesize_report(str(committed), str(path))["status"]
        == "match"
    )
    script_path.write_bytes(b"strash\nmap -a\n")
    check = synthesize.check_synthesize_report(str(committed), str(path))
    assert [c["field"] for c in check["checks"] if not c["match"]] == ["synthesis"]


def test_original_script_golden(tmp_path):
    out = tmp_path / "golden.ys"
    synthesize._write_script(
        script_path=str(out),
        sources=["unit.v"],
        hdl_toplevel="unit",
        liberty_path="cells.lib",
        netlist_path="unit_net.v",
        stats_path="out.json",
    )
    assert out.read_bytes() == (
        b"read_liberty -lib cells.lib\nread_verilog unit.v\n"
        b"hierarchy -check -top unit\n"
        b"synth -top unit\ndfflibmap -liberty cells.lib\n"
        b"abc -liberty cells.lib\nclean\n"
        b"tee -q -o out.json stat -liberty cells.lib -json -top unit\n"
        b"write_verilog -noattr unit_net.v\n"
    )


def test_mapping_changed_from_legacy_is_drift(tmp_path, monkeypatch):
    path = _setup(tmp_path, monkeypatch, {"abc_fast": True})
    request = json.loads(path.read_text())
    del request["synthesis"]
    path.write_text(json.dumps(request))
    legacy = synthesize.run_synthesize(str(path))
    assert "synthesis" not in legacy
    assert "abc_script" not in legacy["provenance"]
    committed = tmp_path / "committed.json"
    committed.write_text(json.dumps(legacy))
    for mapping, expected in (
        ({"abc_fast": False}, "match"),
        ({"abc_fast": True}, "drifted"),
    ):
        request["synthesis"] = mapping
        path.write_text(json.dumps(request))
        assert (
            synthesize.check_synthesize_report(str(committed), str(path))["status"]
            == expected
        )


def test_custom_mapping_from_stdin_resolves_against_cwd(tmp_path, monkeypatch, capsys):
    path = _setup(tmp_path, monkeypatch, {"abc_script": "+custom script"})
    (tmp_path / "+custom script").write_text("strash\nmap\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(path.read_text()))
    assert main(["synthesize", "-", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["synthesis"]["abc_script"]["path"] == "+custom script"


def test_signoff_synthesis_citation_discloses_effort(tmp_path):
    envelope = _synthesize_envelope()
    assert "synthesis" not in _synth_item_1(tmp_path, envelope)["citation"]
    envelope["synthesis"] = {
        "abc_fast": True,
        "abc_script": None,
        "reduced_passes": ["synth_internal", "liberty"],
    }
    item = _synth_item_1(tmp_path, envelope)
    assert item["status"] == "met"
    assert item["citation"]["synthesis"] == envelope["synthesis"]


@pytest.mark.skipif(
    not HAVE_YOSYS or _REAL_SKY130_VARIANT is None,
    reason="requires real Yosys and Sky130 HD liberty",
)
def test_integration_real_yosys_fast_unrolled_divider(tmp_path):
    root, variant = _REAL_SKY130_VARIANT
    rtl = tmp_path / "divider.v"
    rtl.write_text("""module divider(input [31:0] numerator, denominator,
               output reg [31:0] quotient);
  integer i;
  reg [32:0] remainder;
  always @* begin
    remainder = 0;
    quotient = 0;
    for (i = 31; i >= 0; i = i - 1) begin
      remainder = (remainder << 1) | numerator[i];
      if (remainder >= {1'b0,denominator}) begin
        remainder = remainder - {1'b0,denominator};
        quotient[i] = 1'b1;
      end
    end
  end
endmodule
""")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            _base_request(
                sources=[rtl.name], hdl_toplevel="divider", synthesis={"abc_fast": True}
            )
        )
    )
    report = synthesize.run_synthesize(str(request), pdk_root=root, pdk_variant=variant)
    assert report["status"] == "ok"
    assert report["structural"]["has_critical"] is False
    assert report["instance_count"] > 100
    assert all(
        cell.startswith("sky130_fd_sc_hd__")
        for cell in report["instance_counts_by_type"]
    )
    assert report["netlist_sha256"]


def test_inline_fast_mapping_with_delay_target():
    from klayout_tools.synthesize_abc import liberty_command

    command = liberty_command(
        "cells.lib", "inline", None, constrained=True, delay_ps=400
    )
    assert command == (
        "abc -liberty cells.lib -script +strash;dretime;retime,-o,-D,400;"
        "map,-D,400;buffer;upsize,-D,400;dnsize,-D,400;stime,-p"
    )


def test_equivalence_rerun_compares_verdict_and_ignores_duration():
    from klayout_tools.evidence_canonicalize import rerun_result_for

    old = {"equivalence": {"status": "equivalent", "elapsed_s": 0.1}}
    fresh = {"equivalence": {"status": "equivalent", "elapsed_s": 0.5}}
    assert (
        rerun_result_for("synthesize", "report", committed=old, fresh=fresh)["status"]
        == "match"
    )
    fresh["equivalence"]["status"] = "counterexample"
    assert (
        rerun_result_for("synthesize", "report", committed=old, fresh=fresh)["status"]
        == "drifted"
    )


@pytest.mark.parametrize(
    "recorded",
    [
        [],
        {"abc_fast": 1},
        {"abc_fast": False, "abc_script": {}},
        {"abc_fast": True, "abc_script": None},
    ],
)
def test_malformed_or_fast_committed_effort_never_matches_default(recorded):
    assert synthesize_abc.mapping_check(recorded, None)["match"] is False


def test_custom_script_read_and_stage_errors(tmp_path, monkeypatch):
    source = tmp_path / "source.abc"
    source.write_bytes(b"map")
    mapping = synthesize_abc.resolve_mapping(
        {"synthesis": {"abc_script": source.name}},
        str(tmp_path / "request.json"),
        synthesize.SynthesizeError,
    )
    with pytest.raises(synthesize.SynthesizeError, match="could not stage"):
        mapping.stage_script(str(tmp_path / "missing"), synthesize.SynthesizeError)

    def refused(path):
        raise PermissionError("read denied")

    monkeypatch.setattr(Path, "read_bytes", refused)
    with pytest.raises(synthesize.SynthesizeError, match="could not read"):
        synthesize_abc.resolve_mapping(
            {"synthesis": {"abc_script": source.name}},
            str(tmp_path / "request.json"),
            synthesize.SynthesizeError,
        )


def test_inline_fast_without_constraints_has_no_sizing():
    assert (
        synthesize_abc.liberty_command(
            "cells.lib", "inline", None, constrained=False, delay_ps=None
        )
        == "abc -liberty cells.lib -script +strash;dretime;map"
    )
