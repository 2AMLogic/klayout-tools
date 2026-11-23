"""Exercise the generic ABC watchdog through the synthesize and signoff contracts."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import synthesize, synthesize_arithmetic, synthesize_timeout
from klayout_tools.cli import main
from test_signoff import _synth_item_1
from test_synthesize import (
    _REAL_SKY130_VARIANT,
    HAVE_YOSYS,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def setup_run(tmp_path, monkeypatch, **fields):
    monkeypatch.delenv("KLT_YOSYS_TIMEOUT_S", raising=False)
    (tmp_path / ".git").mkdir()
    path = Path(_setup_success_env(tmp_path, monkeypatch))
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))
    _stub_yosys_success(monkeypatch, yosys_log="Warning: checkpoint warning\n")
    return str(path)


def intercept_stage(monkeypatch, *, fail_stage="abc", trial=None, timeout=True):
    run = synthesize.run_in_group
    calls = []

    def spawn(cmd, **kwargs):
        script = Path(cmd[2]).read_text()
        if "synth -noabc" in script:
            stage = "front"
        elif "read_rtlil" in script and "write_rtlil" in script:
            stage = "abc"
        elif "read_rtlil" in script:
            stage = "tail"
        else:
            return run(cmd, **kwargs)
        calls.append((stage, kwargs["timeout"], kwargs["cwd"]))
        assert "$PDK_ROOT" not in script
        if stage == fail_stage and (trial is None or Path(cmd[2]).parent.name == trial):
            if timeout:
                raise subprocess.TimeoutExpired(
                    cmd, kwargs["timeout"], output=b"Warning: interrupted\n"
                )
            return fake_completed(returncode=1, stderr="ERROR: engine failed")
        for line in script.splitlines():
            if line.startswith("write_rtlil "):
                checkpoint = Path(kwargs["cwd"] or ".") / line.removeprefix(
                    "write_rtlil "
                )
                checkpoint.write_text("fixture RTLIL\n")
        if stage == "abc":
            return fake_completed(stdout="Warning: generic ABC warning\n")
        return run(cmd, **kwargs)

    monkeypatch.setattr(synthesize, "run_in_group", spawn)
    return calls


@pytest.mark.parametrize(
    "value", [True, "1", float("nan"), float("inf"), 0, -1, 3601, None]
)
def test_invalid_budget(tmp_path, monkeypatch, capsys, value):
    path = setup_run(tmp_path, monkeypatch, synthesis={"abc_timeout_s": value})
    assert main(["synthesize", path, "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert (
        "request.synthesis.abc_timeout_s must be a finite number > 0 and <= 3600"
        in message
    )


def test_timeout_partial_and_cheap_check(tmp_path, monkeypatch, capsys):
    path = setup_run(
        tmp_path, monkeypatch, synthesis={"abc_timeout_s": 0.25}, run_id="bounded"
    )
    calls = intercept_stage(monkeypatch)
    assert main(["synthesize", path, "--verify-equivalence", "--format", "json"]) == 4
    report = json.loads(capsys.readouterr().out)
    assert calls == [("front", 300, str(tmp_path)), ("abc", 0.25, str(tmp_path))]
    assert report["run_id"] == "bounded"
    assert report["status"] == "inconclusive" and report["reason"] == "abc_timeout"
    assert {"netlist_sha256", "structural", "area_um2", "timing"}.isdisjoint(report)
    assert report["provenance"]["abc_timeout_s"] == 0.25
    assert "abc_timeout_s" not in report["synthesis"]
    assert report["equivalence"]["status"] == "not_run"
    assert report["warnings"]["total"] == 2
    partial = report["partial"]
    assert partial["stage"] == "pre_abc" and partial["abc_timeout_s"] == 0.25
    for field in ("stats_path", "netlist_path"):
        assert partial[field]["scope"] == "repo"
        assert (tmp_path / partial[field]["path"]).is_file()
    stats = json.loads((tmp_path / partial["stats_path"]["path"]).read_text())
    assert partial["cell_counts"] == stats["modules"]["\\gcd"]["num_cells_by_type"]
    item = _synth_item_1(tmp_path, report)
    assert (item["status"], item["reason"]) == ("unmet", "unrecognized_envelope")
    saved = tmp_path / "partial.json"
    saved.write_text(json.dumps(report))
    assert main(["synthesize", path, "--check", str(saved), "--format", "json"]) == 3
    check = json.loads(capsys.readouterr().out)
    assert check["status"] == "drifted"
    assert [c["field"] for c in check["checks"] if not c["match"]] == ["netlist_sha256"]


@pytest.mark.parametrize("suffix", [".v", ".json", ".il"])
def test_missing_checkpoint_is_an_engine_error(tmp_path, monkeypatch, suffix):
    path = setup_run(tmp_path, monkeypatch, synthesis={"abc_timeout_s": 1})
    intercept_stage(monkeypatch)
    run = synthesize._run_yosys

    def discard_artifact(script, **kwargs):
        log = run(script, **kwargs)
        if ".pre_abc." in Path(script).name:
            (Path(script).parent / f"gcd_pre_abc{suffix}").unlink()
        return log

    monkeypatch.setattr(synthesize, "_run_yosys", discard_artifact)
    with pytest.raises(synthesize.SynthesizeError, match="checkpoint"):
        synthesize.run_synthesize(path)


def test_no_budget_report_golden(tmp_path, monkeypatch, capsys):
    # Pin host-dependent fields. The golden includes the digest added in #7277.
    path = setup_run(tmp_path, monkeypatch, run_id="golden")
    monkeypatch.setattr(synthesize, "build_provenance", lambda **kw: {})
    monkeypatch.setattr(synthesize, "_read_sta_timing", lambda *args: (None, "fixture"))
    assert main(["synthesize", path, "--format", "json"]) == 0
    assert (
        hashlib.sha256(capsys.readouterr().out.encode()).hexdigest()
        == "e04454a006ac969f71cd1703d13c5569c62c23da5aad4b85c5ccebc7a468773a"
    )


@pytest.mark.parametrize(
    ("stage", "timeout"),
    [
        ("front", True),
        ("tail", True),
        ("front", False),
        ("abc", False),
        ("tail", False),
    ],
)
def test_engine_failures_remain_errors(tmp_path, monkeypatch, capsys, stage, timeout):
    path = setup_run(tmp_path, monkeypatch, synthesis={"abc_timeout_s": 1})
    intercept_stage(monkeypatch, fail_stage=stage, timeout=timeout)
    assert main(["synthesize", path, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    message = json.loads(captured.err)["error"]["message"]
    assert ("did not complete" if timeout else "engine failed") in message


def test_partial_text_and_rerun(tmp_path, monkeypatch, capsys):
    # The budget keys live in the timeout module's set; minted per-run paths
    # like stats_path live in synthesize's (#7315). The merged set the rerun
    # canonicalizer strips is the union of the two.
    assert synthesize_timeout.RERUN_BOOKKEEPING_KEYS == {
        "yosys_timeout_s",
        "abc_timeout_s",
    }
    assert "stats_path" in synthesize.RERUN_BOOKKEEPING_KEYS
    assert "netlist_path" in synthesize.RERUN_BOOKKEEPING_KEYS
    path = setup_run(tmp_path, monkeypatch, synthesis={"abc_timeout_s": 5})
    with monkeypatch.context() as patch:
        intercept_stage(patch)
        assert main(["synthesize", path, "--verify-equivalence"]) == 4
        text = capsys.readouterr().out
        assert all(
            word in text
            for word in [
                "inconclusive",
                "abc_timeout",
                "pre_abc",
                "netlist_path",
                "stats_path",
                "equivalence: not_run",
            ]
        )
        lines = text.splitlines()
        stats_index = next(
            i for i, line in enumerate(lines) if line.startswith("stats_path:")
        )
        assert lines[stats_index + 1] == "cell_counts:"
        assert lines[stats_index + 2].startswith("  ")
        report = synthesize.run_synthesize(path)
        saved = tmp_path / "saved.json"
        report["provenance"]["abc_timeout_s"] = report["partial"]["abc_timeout_s"] = 10
        saved.write_text(json.dumps(report))
        check = synthesize.rerun_synthesize_report(str(saved), path)
        assert check["fresh"]["run_id"] != report["run_id"]
        assert check["status"] == "match"
    calls = intercept_stage(monkeypatch, fail_stage=None)
    check = synthesize.rerun_synthesize_report(str(saved), path)
    assert check["fresh"]["status"] == "ok"
    assert check["status"] == "drifted"
    assert check["fresh"]["provenance"]["abc_timeout_s"] == 5
    assert check["fresh"]["warnings"]["by_category"]["other"] == 3
    assert calls == [
        ("front", 300, str(tmp_path)),
        ("abc", 5, str(tmp_path)),
        ("tail", 300, str(tmp_path)),
    ]


def test_auto_disqualifies_only_the_timed_out_trial(tmp_path, monkeypatch):
    path = setup_run(
        tmp_path,
        monkeypatch,
        synthesis={"abc_timeout_s": 2},
        arithmetic={"adders": "auto", "candidates": ["ripple"], "verify_adders": False},
    )
    monkeypatch.setattr(synthesize_arithmetic, "_probe_add_widths", lambda **kw: [16])
    calls = intercept_stage(monkeypatch, trial="ripple")
    report = synthesize.run_synthesize(path)
    assert report["status"] == "ok"
    rows = {row["architecture"]: row for row in report["arithmetic"]["candidates"]}
    assert rows["ripple"]["measured"] is None
    assert report["arithmetic"]["selected_architecture"] == "default"
    assert rows["default"]["measured"] is not None
    assert [budget for stage, budget, _ in calls if stage == "abc"] == [2, 2, 2]


requires_sky130 = pytest.mark.skipif(
    not HAVE_YOSYS or _REAL_SKY130_VARIANT is None,
    reason="requires Yosys and Sky130 HD liberty",
)


def real_run(path, **opts):
    root, variant = _REAL_SKY130_VARIANT
    return synthesize.run_synthesize(
        str(path), pdk_root=root, pdk_variant=variant, **opts
    )


@requires_sky130
@pytest.mark.parametrize("mapping", [{}, {"abc_fast": True}, {"abc_script": "map.abc"}])
def test_live_staged_mapping_with_library_instance(tmp_path, mapping):
    (tmp_path / ".git").mkdir()
    (tmp_path / "map.abc").write_text("strash\nmap\n")
    (
        tmp_path / "adder.v"
    ).write_text("""module adder(input [3:0] a,b, output [4:0] y, output z);
  assign y = a + b;
  sky130_fd_sc_hd__inv_1 keep_inv (.A(a[0]), .Y(z));
endmodule
""")
    path = tmp_path / "request.json"
    request = _base_request(
        sources=["adder.v"], hdl_toplevel="adder", synthesis=mapping
    )
    path.write_text(json.dumps(request))
    control = real_run(path)
    path.write_text(
        json.dumps({**request, "synthesis": {**mapping, "abc_timeout_s": 60}})
    )
    staged = real_run(path)
    assert staged["status"] == control["status"] == "ok"
    assert staged["instance_counts_by_type"] == control["instance_counts_by_type"]
    assert staged["netlist_sha256"] == control["netlist_sha256"]
    assert staged["area_um2"] == control["area_um2"]
    assert staged["structural"] == control["structural"]
    assert staged["synthesis"] == control["synthesis"]
    for report in [control, staged]:
        mapped = (tmp_path / report["netlist_path"]["path"]).read_text()
        assert "sky130_fd_sc_hd__inv_1 keep_inv" in mapped


@requires_sky130
@pytest.mark.slow
def test_live_timeout_on_64_bit_restoring_divider(tmp_path):
    (tmp_path / ".git").mkdir()
    (
        tmp_path / "divider.v"
    ).write_text("""module divider(input [63:0] a,b, output reg [63:0] q);
  integer i;
  reg [64:0] rem;
  always @* begin
    rem = 0;
    q = 0;
    for (i = 63; i >= 0; i = i - 1) begin
      rem = (rem << 1) | a[i];
      if (rem >= {1'b0,b}) begin
        rem = rem - {1'b0,b};
        q[i] = 1'b1;
      end
    end
  end
endmodule
""")
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            _base_request(
                sources=["divider.v"],
                hdl_toplevel="divider",
                synthesis={"abc_timeout_s": 1},
            )
        )
    )
    report = real_run(path)
    assert (report["status"], report["reason"]) == ("inconclusive", "abc_timeout")
    partial = report["partial"]
    assert sum(partial["cell_counts"].values()) > 1000
    assert "module divider" in (tmp_path / partial["netlist_path"]["path"]).read_text()
    assert "modules" in json.loads(
        (tmp_path / partial["stats_path"]["path"]).read_text()
    )
