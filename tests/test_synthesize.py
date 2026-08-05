"""Tests for `klt synthesize` and the `klayout_tools.synthesize` library.

Three tiers:

- **Request/library unit tests** exercise `load_request`/`run_synthesize`'s
  error paths (bad request, unreadable RTL source, unresolvable
  `pdk.cell_library`/`corner`) directly, against **fabricated** open_pdks-
  layout PDK installs created under `tmp_path` (never a real PDK -- the
  `_isolate_pdk` helper scrubs the environment and empties `pdk.py`'s
  search-space constants, mirroring `tests/test_pdk.py`'s own hermetic
  posture).
- **Stubbed-Yosys tests** run `run_synthesize` end to end with
  `synthesize.subprocess.run` replaced by a fake that writes the same
  `stat -liberty ... -json` shape a real Yosys run produces (parsed from the
  generated `.ys` script's own `tee`/`write_verilog` lines, so the stub
  never hardcodes this module's private path-naming scheme) -- no `yosys`
  binary required, covering a successful synthesis, a Yosys/ABC elaboration
  error, and a missing-binary error.
- **Integration test** (`@pytest.mark.skipif` when either `yosys` is not on
  `$PATH` or no real PDK install resolving a `sky130_fd_sc_hd` liberty is
  found) runs the *real* GCD worked example from
  `docs/design/yosys-synthesis-spike.md` section 4 end to end and asserts
  the exact numbers that survey's live Yosys run reported (335 instances,
  2951.5808 um^2, 1251.2 um^2 sequential) -- this is the acceptance
  criterion's "verified end to end against a real sky130 install" check;
  it is not required for CI (CI installs neither `yosys` nor a real sky130
  standard-cell PDK today, matching the Yosys survey's own noted CI gap),
  but runs (and passes) on any machine with both.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize
from klayout_tools.cli import main
from klayout_tools.synthesize import SynthesizeError, load_request, run_synthesize

_GCD_RTL = """\
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire              start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

    reg [WIDTH-1:0] a, b;
    reg             busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a      <= {WIDTH{1'b0}};
            b      <= {WIDTH{1'b0}};
            busy   <= 1'b0;
            done   <= 1'b0;
            result <= {WIDTH{1'b0}};
        end else if (start && !busy) begin
            a      <= a_in;
            b      <= b_in;
            busy   <= 1'b1;
            done   <= 1'b0;
        end else if (busy) begin
            if (b == {WIDTH{1'b0}}) begin
                busy   <= 1'b0;
                done   <= 1'b1;
                result <= a;
            end else if (a > b) begin
                a <= a - b;
            end else begin
                b <= b - a;
            end
        end else begin
            done <= 1'b0;
        end
    end

endmodule
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _base_request(**overrides) -> dict:
    request = {
        "engine": "yosys",
        "sources": ["gcd.v"],
        "hdl_toplevel": "gcd",
        "pdk": {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"},
    }
    request.update(overrides)
    return request


def _isolate_pdk(monkeypatch, tmp_path: Path) -> None:
    """Scrub PDK env vars and empty `pdk.py`'s search-space constants, so
    `find_pdk()` only ever resolves what a test explicitly points it at
    (mirrors `tests/test_pdk.py`'s `_isolate` fixture). Not autouse: the
    real-PDK integration test below deliberately runs *without* this."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pdk_module, "STORE_DIRS", [])
    monkeypatch.setattr(pdk_module, "CONVENTIONAL_PREFIXES", [])


def _make_pdk_install(
    root: Path,
    variant: str,
    *,
    cell_library: str = "sky130_fd_sc_hd",
    corners: tuple[tuple[str, float, float, float], ...] = (
        ("tt_025C_1v80", 1.0, 25.0, 1.8),
    ),
    with_cell_library: bool = True,
) -> Path:
    """Fabricate a minimal open_pdks-layout variant under ``root`` with a
    standard-cell library's `lib/` view(s) -- enough for `find_pdk()`/
    `list_cell_libraries()`/`run_synthesize`'s own liberty resolution, not a
    real, Yosys-parseable liberty file (the stubbed-Yosys tests never invoke
    a real Yosys against it)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    libs_ref = variant_dir / "libs.ref"
    libs_ref.mkdir(parents=True, exist_ok=True)
    if with_cell_library:
        lib_views_dir = libs_ref / cell_library / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        for corner_name, process, temperature, voltage in corners:
            content = (
                f'    default_operating_conditions : "{corner_name}";\n'
                f"    nom_process : {process};\n"
                f"    nom_temperature : {temperature};\n"
                f"    nom_voltage : {voltage};\n"
            )
            (lib_views_dir / f"{cell_library}__{corner_name}.lib").write_text(
                content, encoding="utf-8"
            )
    return variant_dir


# --------------------------------------------------------------------------- #
# `load_request`
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(SynthesizeError, match="file not found"):
        load_request(str(tmp_path / "nope.json"))


def test_load_request_directory(tmp_path):
    with pytest.raises(SynthesizeError, match="not a file"):
        load_request(str(tmp_path))


def test_load_request_invalid_json(tmp_path):
    path = _write(tmp_path / "request.json", "{not json")
    with pytest.raises(SynthesizeError, match="not valid JSON"):
        load_request(path)


def test_load_request_not_an_object(tmp_path):
    path = _write(tmp_path / "request.json", "[1, 2, 3]")
    with pytest.raises(SynthesizeError, match="must contain a JSON object"):
        load_request(path)


@pytest.mark.parametrize("field", ["sources", "hdl_toplevel", "pdk"])
def test_load_request_missing_required_field(tmp_path, field):
    request = _base_request()
    del request[field]
    path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(SynthesizeError, match=f"missing required field: {field}"):
        load_request(path)


# --------------------------------------------------------------------------- #
# `run_synthesize` request validation (no PDK/Yosys involved)
# --------------------------------------------------------------------------- #


def test_run_synthesize_unsupported_engine(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(engine="verific")
    )
    with pytest.raises(SynthesizeError, match="unsupported engine 'verific'"):
        run_synthesize(request_path)


def test_run_synthesize_sources_must_be_nonempty_array(tmp_path):
    request_path = _write_request(tmp_path / "request.json", _base_request(sources=[]))
    with pytest.raises(SynthesizeError, match="non-empty array"):
        run_synthesize(request_path)


def test_run_synthesize_sources_entries_must_be_strings(tmp_path):
    request_path = _write_request(tmp_path / "request.json", _base_request(sources=[1]))
    with pytest.raises(SynthesizeError, match="non-empty strings"):
        run_synthesize(request_path)


def test_run_synthesize_missing_rtl_source(tmp_path):
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )
    with pytest.raises(SynthesizeError, match="RTL source not found: nope.v"):
        run_synthesize(request_path)


def test_run_synthesize_unreadable_rtl_source(tmp_path):
    source_path = tmp_path / "gcd.v"
    source_path.write_text(_GCD_RTL, encoding="utf-8")
    os.chmod(source_path, 0o000)
    try:
        request_path = _write_request(tmp_path / "request.json", _base_request())
        with pytest.raises(SynthesizeError, match="could not read RTL source"):
            run_synthesize(request_path)
    finally:
        os.chmod(source_path, 0o644)


def test_run_synthesize_hdl_toplevel_must_be_nonempty_string(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(hdl_toplevel="")
    )
    with pytest.raises(SynthesizeError, match="hdl_toplevel must be"):
        run_synthesize(request_path)


def test_run_synthesize_pdk_must_be_object(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(tmp_path / "request.json", _base_request(pdk="x"))
    with pytest.raises(SynthesizeError, match="request.pdk must be a JSON object"):
        run_synthesize(request_path)


def test_run_synthesize_cell_library_required(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(pdk={"corner": "tt_025C_1v80"})
    )
    with pytest.raises(SynthesizeError, match="pdk.cell_library is required"):
        run_synthesize(request_path)


def test_run_synthesize_corner_must_be_nonempty_string(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "sky130_fd_sc_hd", "corner": 5}),
    )
    with pytest.raises(SynthesizeError, match="pdk.corner must be"):
        run_synthesize(request_path)


def test_run_synthesize_constraints_must_be_object(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(constraints="x")
    )
    with pytest.raises(SynthesizeError, match="request.constraints must be"):
        run_synthesize(request_path)


# --------------------------------------------------------------------------- #
# Liberty resolution (fabricated PDK installs, `find_pdk()` involved)
# --------------------------------------------------------------------------- #


def test_run_synthesize_no_pdk_installed(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(SynthesizeError, match="no supported-layout PDK install"):
        run_synthesize(request_path)


def test_run_synthesize_cell_library_not_shipped(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A", with_cell_library=False)
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(
        SynthesizeError,
        match=r"liberty not found for deck.*sky130_fd_sc_hd.*not found under",
    ):
        run_synthesize(request_path)


def test_run_synthesize_corner_not_shipped(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "sky130_fd_sc_hd", "corner": "ff_n40C_1v95"}
        ),
    )
    with pytest.raises(
        SynthesizeError, match=r"liberty not found for deck.*ff_n40C_1v95"
    ):
        run_synthesize(request_path)


def test_run_synthesize_nominal_corner_default(tmp_path, monkeypatch):
    """Omitting `pdk.corner` resolves the nominal (typical-process,
    room-temperature) corner via `list_cell_libraries()`'s own
    `nominal_corner` selection -- verified here via `_resolve_liberty`
    directly (no Yosys/subprocess involved)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "sky130A",
        corners=(
            ("tt_025C_1v80", 1.0, 25.0, 1.8),
            ("ff_n40C_1v95", 1.0, -40.0, 1.95),
        ),
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    liberty_path, corner, info = synthesize._resolve_liberty("sky130_fd_sc_hd", None)
    assert corner == "tt_025C_1v80"
    assert liberty_path.endswith("sky130_fd_sc_hd__tt_025C_1v80.lib")
    assert info["variant"] == "sky130A"


# --------------------------------------------------------------------------- #
# Stubbed Yosys: full `run_synthesize` without a real `yosys` binary
# --------------------------------------------------------------------------- #

_TEE_RE = re.compile(r"^tee -q -o (\S+) ")
_WRITE_VERILOG_RE = re.compile(r"^write_verilog -noattr (\S+)$")

#: Trimmed, but numerically real, `stat -liberty ... -json`-shaped stats for
#: the GCD worked example -- Yosys survey section 4's own live-captured
#: numbers, reused here as the stub payload so the parsing logic is checked
#: against byte-accurate real data without requiring a real Yosys binary.
_GCD_MODULE_STATS = {
    "num_wires": 295,
    "num_cells": 335,
    "area": 2951.5808,
    "sequential_area": 1251.2,
    "num_cells_by_type": {
        "sky130_fd_sc_hd__xor2_1": 6,
        "sky130_fd_sc_hd__a211o_1": 1,
        "sky130_fd_sc_hd__dfrtp_1": 50,
    },
}


def _script_output_paths(script_path: str) -> tuple[str, str]:
    stats_path = None
    netlist_path = None
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            match = _TEE_RE.match(line)
            if match:
                stats_path = match.group(1)
            match = _WRITE_VERILOG_RE.match(line)
            if match:
                netlist_path = match.group(1)
    assert stats_path is not None and netlist_path is not None, (
        "generated .ys script is missing a `tee -q -o`/`write_verilog` line"
    )
    return stats_path, netlist_path


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_yosys_success(
    monkeypatch,
    *,
    hdl_toplevel: str = "gcd",
    module_stats: dict | None = None,
    version: str = "0.67+post",
) -> None:
    stats = module_stats if module_stats is not None else _GCD_MODULE_STATS

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-V"]:
            return _FakeCompleted(
                stdout=f"Yosys {version} (git sha1 deadbeef, Release)\n"
            )
        assert cmd[:2] == ["yosys", "-s"]
        script_path = cmd[2]
        stats_path, netlist_path = _script_output_paths(script_path)
        with open(stats_path, "w", encoding="utf-8") as handle:
            json.dump({"modules": {f"\\{hdl_toplevel}": stats}}, handle)
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write("// fake mapped netlist\n")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)


def _setup_success_env(tmp_path, monkeypatch) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    return _write_request(tmp_path / "request.json", _base_request())


def test_run_synthesize_stubbed_success(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "yosys"
    assert report["engine_version"] == "0.67+post"
    assert report["hdl_toplevel"] == "gcd"
    assert report["status"] == "ok"
    assert report["instance_count"] == 335
    assert report["area_um2"] == 2951.5808
    assert report["sequential_area_um2"] == 1251.2
    assert list(report["instance_counts_by_type"]) == sorted(
        report["instance_counts_by_type"]
    )
    assert report["instance_counts_by_type"]["sky130_fd_sc_hd__dfrtp_1"] == 50
    assert report["timing"] is None

    assert os.path.isabs(report["netlist_path"])
    assert os.path.isfile(report["netlist_path"])
    assert os.path.isabs(report["script_path"])
    assert os.path.isfile(report["script_path"])
    assert report["netlist_path"].endswith("gcd_synth.v")
    assert report["script_path"].endswith("synth_gcd.ys")
    assert ".klt/synthesize" in report["netlist_path"].replace(os.sep, "/")

    provenance = report["provenance"]
    assert provenance["klt_version"]
    assert provenance["pdk"]["name"] == "sky130A"
    assert provenance["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"
    assert provenance["deck"]["content_hash"].startswith("sha256:")
    assert provenance["input"]["content_hash"].startswith("sha256:")


def test_run_synthesize_missing_sequential_area_degrades_to_none(tmp_path, monkeypatch):
    """Distro-packaged Yosys (e.g. Ubuntu 24.04's 0.33) omits `sequential_area`
    from `stat -json` entirely -- `run_synthesize` must not raise a `KeyError`
    (#560) and should instead report `sequential_area_um2: None`, leaving every
    other field populated exactly as the Yosys 0.67+ shape does."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    old_yosys_stats = {
        key: value
        for key, value in _GCD_MODULE_STATS.items()
        if key != "sequential_area"
    }
    assert "sequential_area" not in old_yosys_stats
    _stub_yosys_success(monkeypatch, module_stats=old_yosys_stats, version="0.33")

    report = run_synthesize(request_path)

    assert report["engine_version"] == "0.33"
    assert report["instance_count"] == 335
    assert report["area_um2"] == 2951.5808
    assert report["sequential_area_um2"] is None
    assert report["instance_counts_by_type"]["sky130_fd_sc_hd__dfrtp_1"] == 50


def test_run_synthesize_stubbed_success_default_corner(tmp_path, monkeypatch):
    """The response's `provenance.deck.name` reflects the resolved nominal
    corner when `pdk.corner` is omitted from the request."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "sky130_fd_sc_hd"}),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert report["provenance"]["deck"]["name"] == "sky130_fd_sc_hd__tt_025C_1v80"


def test_run_synthesize_stubbed_multi_source_combined_hash(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _write(tmp_path / "helper.v", "module helper(); endmodule\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(sources=["gcd.v", "helper.v"]),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    content_hash = report["provenance"]["input"]["content_hash"]
    assert content_hash.startswith("sha256:")
    # Order-independent: listing the same two sources in the opposite order
    # produces the identical combined hash.
    reordered_request_path = _write_request(
        tmp_path / "request2.json",
        _base_request(sources=["helper.v", "gcd.v"]),
    )
    report2 = run_synthesize(reordered_request_path)
    assert report2["provenance"]["input"]["content_hash"] == content_hash


def test_run_synthesize_stubbed_elaboration_error(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["yosys", "-s"]
        return _FakeCompleted(
            returncode=1, stderr="ERROR: Module `not_a_module' not found!\n"
        )

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match=r"yosys synthesis failed:.*not_a_module"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_generic_engine_failure(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["yosys", "-s"]
        return _FakeCompleted(returncode=2, stdout="something went wrong\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match="yosys exited with code 2"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_missing_binary(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: yosys")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match="could not launch yosys"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_missing_netlist_output(tmp_path, monkeypatch):
    """Defensive check: a `yosys -s` run that exits 0 but never wrote the
    declared netlist output is still a failure, not a silent partial
    success."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(stdout="Yosys 0.67+post\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match="did not produce"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_missing_stats_output(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            script_path = cmd[2]
            _, netlist_path = _script_output_paths(script_path)
            with open(netlist_path, "w", encoding="utf-8") as handle:
                handle.write("// fake netlist, no stats written\n")
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(stdout="Yosys 0.67+post\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match="did not produce the expected stats"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_stats_missing_top_module(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            script_path = cmd[2]
            stats_path, netlist_path = _script_output_paths(script_path)
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"modules": {"\\some_other_module": _GCD_MODULE_STATS}}, handle
                )
            with open(netlist_path, "w", encoding="utf-8") as handle:
                handle.write("// fake netlist\n")
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(stdout="Yosys 0.67+post\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match="could not find synthesis statistics"):
        run_synthesize(request_path)


def test_run_synthesize_stubbed_engine_version_unresolvable(tmp_path, monkeypatch):
    """`engine_version` degrades to `None`, never raising, when `yosys -V`
    itself can't be resolved (e.g. the binary vanished between the two
    subprocess calls)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            script_path = cmd[2]
            stats_path, netlist_path = _script_output_paths(script_path)
            with open(stats_path, "w", encoding="utf-8") as handle:
                json.dump({"modules": {"\\gcd": _GCD_MODULE_STATS}}, handle)
            with open(netlist_path, "w", encoding="utf-8") as handle:
                handle.write("// fake netlist\n")
            return _FakeCompleted(returncode=0)
        raise FileNotFoundError("no yosys")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    report = run_synthesize(request_path)
    assert report["engine_version"] is None


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_success_exits_zero_json(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    exit_code = main(["synthesize", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["instance_count"] == 335


def test_cli_text_default_format(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    exit_code = main(["synthesize", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert "instance_count: 335" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_error_exits_one_with_json_error(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )

    exit_code = main(["synthesize", request_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "synthesize"
    assert "RTL source not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )

    exit_code = main(["synthesize", request_path])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt synthesize:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["synthesize"])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# Integration: real Yosys + a real, host-resolved sky130 PDK
# (skipped when either is unavailable -- never required for CI, see this
# module's own docstring)
# --------------------------------------------------------------------------- #

HAVE_YOSYS = shutil.which("yosys") is not None


def _find_real_sky130_variant() -> tuple[str, str] | None:
    """Search *every* install/variant `list_pdks()` discovers (not just
    `find_pdk()`'s own default-variant pick, which is alphabetical-first and
    would land on a non-sky130 variant on a machine with multiple PDK
    families installed, e.g. gf180mcu*) for one shipping a real
    `sky130_fd_sc_hd` liberty. Returns ``(root, variant)`` or ``None``."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            candidate = os.path.join(
                install["root"], variant["name"], "libs.ref", "sky130_fd_sc_hd", "lib"
            )
            if os.path.isdir(candidate):
                return install["root"], variant["name"]
    return None


_REAL_SKY130_VARIANT = _find_real_sky130_variant()


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None,
    reason="no real sky130_fd_sc_hd liberty resolves via list_pdks() on this machine",
)
def test_integration_real_yosys_gcd_worked_example(tmp_path, monkeypatch):
    """The exact worked example from `docs/design/yosys-synthesis-spike.md`
    section 4, run against a real Yosys binary and a real, host-resolved
    sky130 PDK install -- asserts the survey's own live-captured numbers."""
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    request_path = _setup_success_env_real(tmp_path)

    report = run_synthesize(request_path)

    assert report["status"] == "ok"
    assert report["engine"] == "yosys"
    assert report["instance_count"] == 335
    assert report["area_um2"] == pytest.approx(2951.5808)
    assert report["sequential_area_um2"] == pytest.approx(1251.2)
    assert report["instance_counts_by_type"]["sky130_fd_sc_hd__dfrtp_1"] == 50
    assert report["timing"] is None
    assert os.path.isfile(report["netlist_path"])
    assert os.path.isfile(report["script_path"])

    # `write_verilog -noattr`'s own output is a real, non-trivial netlist --
    # sanity-check it names the top module and at least one mapped cell.
    with open(report["netlist_path"], encoding="utf-8") as handle:
        netlist_text = handle.read()
    assert "module gcd" in netlist_text
    assert "sky130_fd_sc_hd__dfrtp_1" in netlist_text


def _setup_success_env_real(tmp_path: Path) -> str:
    _write(tmp_path / "gcd.v", _GCD_RTL)
    return _write_request(tmp_path / "request.json", _base_request())


# Sanity: `subprocess` really is the module this file's stubs patch (guards
# against a future refactor silently making the stubs a no-op).
def test_synthesize_uses_stdlib_subprocess():
    assert synthesize.subprocess is subprocess
