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
  error, and a missing-binary error. The stub also answers the
  `yosys -p 'help abc'` capability probe (issue #807) and writes a real
  `stime -p` summary line into whatever ABC log the generated script
  `tee`s, so the `timing` object's parsing path is exercised too.
- **Integration test** (`@pytest.mark.skipif` when either `yosys` is not on
  `$PATH` or no real PDK install resolving a `sky130_fd_sc_hd` liberty is
  found) runs the *real* GCD worked example from
  `docs/design/yosys-synthesis-spike.md` section 4 end to end -- this is the
  acceptance criterion's "verified end to end against a real sky130 install"
  check; it is not required for CI (CI installs neither `yosys` nor a real
  sky130 standard-cell PDK today, matching the Yosys survey's own noted CI
  gap), but runs on any machine with both. Its build-independent assertions
  (zero excluded cells, a populated `timing` object) always run; the exact
  instance/area numbers `docs/cli/synthesize.md`'s worked example documents
  are asserted only on the Yosys build that produced them, since Yosys's
  own mapping result moves between releases (measured, issue #807: the
  pre-#807 flow maps this design to 335 cells on 0.68 and 326 on Ubuntu
  24.04's 0.33).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize
from klayout_tools.cli import main
from klayout_tools.equiv import EquivError
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
    prefixed_operating_conditions: bool = False,
) -> Path:
    """Fabricate a minimal open_pdks-layout variant under ``root`` with a
    standard-cell library's `lib/` view(s) -- enough for `find_pdk()`/
    `list_cell_libraries()`/`run_synthesize`'s own liberty resolution, not a
    real, Yosys-parseable liberty file (the stubbed-Yosys tests never invoke
    a real Yosys against it).

    ``prefixed_operating_conditions=True`` writes the `.lib` file's
    `default_operating_conditions` attribute as `f"{cell_library}__{corner_name}"`
    instead of the bare ``corner_name`` -- gf180mcu_fd_sc_mcu9t5v0's real
    shape (issue #820); the on-disk filename is unaffected."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    libs_ref = variant_dir / "libs.ref"
    libs_ref.mkdir(parents=True, exist_ok=True)
    if with_cell_library:
        lib_views_dir = libs_ref / cell_library / "lib"
        lib_views_dir.mkdir(parents=True, exist_ok=True)
        for corner_name, process, temperature, voltage in corners:
            operating_conditions = (
                f"{cell_library}__{corner_name}"
                if prefixed_operating_conditions
                else corner_name
            )
            content = (
                f'    default_operating_conditions : "{operating_conditions}";\n'
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


def test_run_synthesize_nominal_corner_default_gf180mcu_shape(tmp_path, monkeypatch):
    """gf180mcu_fd_sc_mcu9t5v0's `.lib` files write their own
    `<cell_library>__` prefix into `default_operating_conditions`, unlike
    sky130's bare attribute. Omitting `pdk.corner` must still resolve to the
    correctly-named on-disk liberty file -- not a doubled-prefix path that
    does not exist (issue #820)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuD",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),),
        prefixed_operating_conditions=True,
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    liberty_path, corner, info = synthesize._resolve_liberty(
        "gf180mcu_fd_sc_mcu9t5v0", None
    )
    assert corner == "tt_025C_1v80"
    assert liberty_path.endswith("gf180mcu_fd_sc_mcu9t5v0__tt_025C_1v80.lib")
    assert info["variant"] == "gf180mcuD"


# --------------------------------------------------------------------------- #
# Stubbed Yosys: full `run_synthesize` without a real `yosys` binary
# --------------------------------------------------------------------------- #

_TEE_RE = re.compile(r"^tee -q -o (\S+) stat ")
_ABC_TEE_RE = re.compile(r"^tee -q -o (\S+) abc ")
_WRITE_VERILOG_RE = re.compile(r"^write_verilog -noattr (\S+)$")

#: A real `abc -constr` run's own `stime -p` summary line as it reaches the
#: Yosys log -- captured verbatim from a live Yosys 0.68+48 run of the GCD
#: worked example against `sky130_fd_sc_hd` (issue #807), so the parser is
#: checked against byte-accurate real output, not an invented shape.
_ABC_STIME_LINE = (
    'ABC: WireLoad = "none"  Gates =    297 (  5.7 %)   Cap =  6.2 ff ( 12.4 %)'
    "   Area =     1986.91 ( 66.3 %)   Delay =  2485.93 ps  ( 32.3 %)               "
)

#: The `-dont_use` fragment of `yosys -p 'help abc'` (Yosys 0.68's own
#: wording, verbatim) -- what the capability probe looks for.
_ABC_HELP_WITH_DONT_USE = """
    -dont_use <cell_name>
        avoid usage of the technology cell <cell_name> when mapping the design.
        this option can be used multiple times with different cell names and
        supports simple glob patterns in the cell name.
        only supported with Liberty cell libraries.
"""

#: Ubuntu 24.04's Yosys 0.33 documents `-constr`/`-D` but has no `-dont_use`
#: option at all (verified live, issue #807) -- passing it there is a hard
#: error, which is why the probe exists.
_ABC_HELP_WITHOUT_DONT_USE = """
    -constr <file>
        pass this file with timing constraints to ABC.
        use with -liberty/-genlib.
"""

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


def _script_abc_log_path(script_path: str) -> str | None:
    """The path the generated script `tee`s the `abc` pass's own output to,
    or `None` when the script does not capture it (no `-constr`)."""
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            match = _ABC_TEE_RE.match(line.rstrip("\n"))
            if match:
                return match.group(1)
    return None


def _script_abc_line(script_path: str) -> str:
    """The generated script's own `abc` line (with any `tee` wrapper)."""
    with open(script_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith("abc ") or _ABC_TEE_RE.match(line):
                return line
    raise AssertionError("generated .ys script has no `abc` line")


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
    supports_dont_use: bool = True,
    abc_log_lines: str | None = None,
) -> None:
    stats = module_stats if module_stats is not None else _GCD_MODULE_STATS
    abc_log = _ABC_STIME_LINE if abc_log_lines is None else abc_log_lines

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-V"]:
            return _FakeCompleted(
                stdout=f"Yosys {version} (git sha1 deadbeef, Release)\n"
            )
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return _FakeCompleted(
                stdout=_ABC_HELP_WITH_DONT_USE
                if supports_dont_use
                else _ABC_HELP_WITHOUT_DONT_USE
            )
        assert cmd[:2] == ["yosys", "-s"]
        script_path = cmd[2]
        stats_path, netlist_path = _script_output_paths(script_path)
        with open(stats_path, "w", encoding="utf-8") as handle:
            json.dump({"modules": {f"\\{hdl_toplevel}": stats}}, handle)
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write("// fake mapped netlist\n")
        abc_log_path = _script_abc_log_path(script_path)
        if abc_log_path is not None:
            with open(abc_log_path, "w", encoding="utf-8") as handle:
                handle.write(abc_log + "\n")
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
    assert report["timing"] == {
        "source": "abc_stime",
        "wire_load": None,
        "critical_path_ps": 2485.93,
        "delay_target_ps": None,
    }

    # The `sta` field (issue #925) always exists in the response shape, but
    # is `None` here: this tier's "netlist" is a fabricated stub, so the
    # native engine has nothing analyzable even when the optional
    # `klt_statime_native` extension happens to be built on this machine.
    # `_read_sta_timing`'s "degrade to None, never fabricate" contract is
    # what keeps a missing/failing engine from breaking synthesis at all --
    # exercised directly in `tests/test_sta.py`.
    assert "sta" in report
    assert report["sta"] is None

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
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return _FakeCompleted(stdout=_ABC_HELP_WITH_DONT_USE)
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
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return _FakeCompleted(stdout=_ABC_HELP_WITH_DONT_USE)
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
# Hierarchical `instance_count`/`instance_counts_by_type` rollup (issue #821)
#
# Every fixture above (`_GCD_MODULE_STATS` included) is single-module: the
# top's own `modules["\\<top>"]` block already *is* the whole design, so the
# pre-#821 direct pass-through happened to be correct for all of them. These
# fixtures instead give the top module a real sub-hierarchy -- shaped like
# the `mac8` (`mult8` + `adder16`, sky130_fd_sc_hd) example captured live
# against real Yosys 0.68+post in issue #821's curation pass -- so a
# regression in the recursive aggregation (right for one level/one instance,
# wrong for 2+ levels or a multiply-instantiated submodule) has something to
# actually be wrong against.
# --------------------------------------------------------------------------- #

#: `adder16`-shaped submodule: 54 leaf standard cells, fully accounted for in
#: its own `num_cells_by_type` (unlike the trimmed `_GCD_MODULE_STATS`) so a
#: test can assert the aggregated by-type dict sums to the aggregated total.
_ADDER16_MODULE_STATS = {
    "num_cells": 54,
    "area": 425.408,
    "num_cells_by_type": {
        "sky130_fd_sc_hd__xor2_1": 32,
        "sky130_fd_sc_hd__a211o_1": 22,
    },
}

#: `mult8`-shaped submodule: 248 leaf standard cells.
_MULT8_MODULE_STATS = {
    "num_cells": 248,
    "area": 1939.36,
    "num_cells_by_type": {
        "sky130_fd_sc_hd__and2_1": 200,
        "sky130_fd_sc_hd__nand2_1": 48,
    },
}

#: `mac8`-shaped 2-level hierarchy: top instantiates both submodules exactly
#: once each and has no cells of its own -- `num_cells: 0` with
#: `num_cells_by_type` naming the two submodules as pseudo cell types,
#: exactly as captured live in issue #821's curation pass. `area` (2364.768)
#: is already `adder16`'s 425.408 + `mult8`'s 1939.36, matching the "area is
#: already a recursive rollup" finding this fix must leave unchanged.
_MAC8_MODULES = {
    "\\mac8": {
        "num_cells": 0,
        "num_submodules": 2,
        "area": 2364.768,
        "num_cells_by_type": {"adder16": 1, "mult8": 1},
    },
    "\\adder16": _ADDER16_MODULE_STATS,
    "\\mult8": _MULT8_MODULE_STATS,
}


def _stub_hierarchical_yosys(
    monkeypatch,
    *,
    modules: dict,
    version: str = "0.68+post",
) -> None:
    """Like `_stub_yosys_success`, but writes a full multi-module `modules`
    dict (already escaped/backslash-keyed) rather than a single top-level
    stats block -- the shape a real hierarchical design's
    `stat -liberty ... -json -top <top>` output actually has."""

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-V"]:
            return _FakeCompleted(
                stdout=f"Yosys {version} (git sha1 deadbeef, Release)\n"
            )
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return _FakeCompleted(stdout=_ABC_HELP_WITH_DONT_USE)
        assert cmd[:2] == ["yosys", "-s"]
        script_path = cmd[2]
        stats_path, netlist_path = _script_output_paths(script_path)
        with open(stats_path, "w", encoding="utf-8") as handle:
            json.dump({"modules": modules}, handle)
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write("// fake mapped netlist\n")
        abc_log_path = _script_abc_log_path(script_path)
        if abc_log_path is not None:
            with open(abc_log_path, "w", encoding="utf-8") as handle:
                handle.write(_ABC_STIME_LINE + "\n")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)


def _setup_hierarchical_env(tmp_path, monkeypatch, hdl_toplevel: str) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "top.v", f"module {hdl_toplevel}(); endmodule\n")
    return _write_request(
        tmp_path / "request.json",
        _base_request(sources=["top.v"], hdl_toplevel=hdl_toplevel),
    )


def test_run_synthesize_hierarchical_top_reports_recursive_instance_count(
    tmp_path, monkeypatch
):
    """The `mac8` fixture (top wraps `mult8` + `adder16`, neither flattened
    by Yosys's default `synth`): `instance_count` must be the true recursive
    total (302, per issue #821's live repro), never the top module's own
    (zero) direct-cell count, and `instance_counts_by_type` must contain only
    real leaf standard-cell types -- never `adder16`/`mult8` as a pseudo cell
    type."""
    request_path = _setup_hierarchical_env(tmp_path, monkeypatch, "mac8")
    _stub_hierarchical_yosys(monkeypatch, modules=_MAC8_MODULES)

    report = run_synthesize(request_path)

    assert report["instance_count"] == 302
    assert report["area_um2"] == 2364.768
    by_type = report["instance_counts_by_type"]
    assert "adder16" not in by_type
    assert "mult8" not in by_type
    assert by_type == {
        "sky130_fd_sc_hd__a211o_1": 22,
        "sky130_fd_sc_hd__and2_1": 200,
        "sky130_fd_sc_hd__nand2_1": 48,
        "sky130_fd_sc_hd__xor2_1": 32,
    }
    assert sum(by_type.values()) == 302


def test_read_stats_multiply_instantiated_submodule_scales_counts(tmp_path):
    """A submodule instantiated twice under the same parent must contribute
    its counts *twice*, not once -- the parent block's own
    `num_cells_by_type` count for that entry (2) is the instance
    multiplicity, verified live against real Yosys output in issue #821."""
    modules = {
        "\\dual_adder": {
            "num_cells": 0,
            "area": 850.816,
            "num_cells_by_type": {"adder16": 2},
        },
        "\\adder16": _ADDER16_MODULE_STATS,
    }
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps({"modules": modules}), encoding="utf-8")

    result = synthesize._read_stats(str(stats_path), "dual_adder")

    assert result["num_cells"] == 108
    assert result["num_cells_by_type"] == {
        "sky130_fd_sc_hd__xor2_1": 64,
        "sky130_fd_sc_hd__a211o_1": 44,
    }
    assert result["area"] == 850.816


def test_read_stats_three_level_hierarchy_recurses_past_one_level(tmp_path):
    """A submodule that itself instantiates a sub-submodule: the recursion
    must not assume exactly one level. `grand` wraps one `mid`, `mid` wraps
    two `leaf` plus 5 direct cells of its own."""
    modules = {
        "\\grand": {
            "num_cells": 0,
            "area": 999.0,
            "num_cells_by_type": {"mid": 1},
        },
        "\\mid": {
            "num_cells": 5,
            "area": 999.0,
            "num_cells_by_type": {
                "sky130_fd_sc_hd__buf_1": 5,
                "leaf": 2,
            },
        },
        "\\leaf": {
            "num_cells": 10,
            "area": 999.0,
            "num_cells_by_type": {"sky130_fd_sc_hd__inv_1": 10},
        },
    }
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps({"modules": modules}), encoding="utf-8")

    result = synthesize._read_stats(str(stats_path), "grand")

    # mid's own 5 direct cells + 2 * leaf's 10 cells = 25.
    assert result["num_cells"] == 25
    assert result["num_cells_by_type"] == {
        "sky130_fd_sc_hd__buf_1": 5,
        "sky130_fd_sc_hd__inv_1": 20,
    }
    assert "mid" not in result["num_cells_by_type"]
    assert "leaf" not in result["num_cells_by_type"]


def test_read_stats_mixed_direct_cells_and_submodule_instances(tmp_path):
    """A top module that is not a pure wrapper -- it has both direct cells of
    its own *and* a submodule instance -- must sum both contributions."""
    modules = {
        "\\mixed_top": {
            "num_cells": 3,
            "area": 999.0,
            "num_cells_by_type": {
                "sky130_fd_sc_hd__inv_1": 3,
                "adder16": 1,
            },
        },
        "\\adder16": _ADDER16_MODULE_STATS,
    }
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps({"modules": modules}), encoding="utf-8")

    result = synthesize._read_stats(str(stats_path), "mixed_top")

    assert result["num_cells"] == 57
    assert result["num_cells_by_type"] == {
        "sky130_fd_sc_hd__inv_1": 3,
        "sky130_fd_sc_hd__xor2_1": 32,
        "sky130_fd_sc_hd__a211o_1": 22,
    }


def test_read_stats_single_module_design_unaffected(tmp_path):
    """A single-module design (no other key in `modules` matches any of its
    own `num_cells_by_type` entries) is unaffected by the #821 fix -- same
    `num_cells`/`num_cells_by_type` values as the pre-#821 direct
    pass-through, even when (as in this trimmed real fixture) the by-type
    dict does not itself sum to `num_cells`."""
    modules = {"\\gcd": _GCD_MODULE_STATS}
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(json.dumps({"modules": modules}), encoding="utf-8")

    result = synthesize._read_stats(str(stats_path), "gcd")

    assert result["num_cells"] == 335
    assert result["num_cells_by_type"] == _GCD_MODULE_STATS["num_cells_by_type"]
    assert result["area"] == 2951.5808
    assert result["sequential_area"] == 1251.2


#: Byte-real `stat -json` output captured live from Yosys 0.33 (Ubuntu's
#: distro build, `git sha1 2584903a060`) while implementing #821, running the
#: exact pass sequence `_write_script` emits over a 3-level design (`top`
#: instantiates one `mid` plus one direct `$_NOT_`; `mid` instantiates two
#: `leaf`; `leaf` holds one `$_NAND_`).
#:
#: Its `num_cells` accounting differs from the Yosys 0.68 capture in #821's
#: curation pass: 0.33 has no `num_submodules` field and *counts each
#: submodule instance in the parent's own `num_cells`* (`top` reports 2 = one
#: `$_NOT_` + one `mid`), whereas 0.68 excludes them (`mac8` reports 0). Any
#: rollup that seeds its total from the parent's own `num_cells` therefore
#: double-counts on 0.33 -- this fixture pins the version-independent
#: behaviour, and the expected values below are Yosys's own `design` rollup
#: from the same capture.
_YOSYS_033_HIER_MODULES = {
    "\\leaf": {
        "num_wires": 3,
        "num_cells": 1,
        "num_cells_by_type": {"$_NAND_": 1},
    },
    "\\mid": {
        "num_wires": 3,
        "num_cells": 2,
        "num_cells_by_type": {"leaf": 2},
    },
    "\\top": {
        "num_wires": 5,
        "num_cells": 2,
        "num_cells_by_type": {"$_NOT_": 1, "mid": 1},
    },
}


def test_read_stats_matches_yosys_own_design_rollup_on_0_33_shape(tmp_path):
    """Against real Yosys 0.33 output -- where a parent's own `num_cells`
    *includes* its submodule instances -- the rollup must reproduce Yosys's
    own `design` block exactly (`num_cells: 3`, `{"$_NAND_": 2, "$_NOT_": 1}`),
    not 6 (the double-count a `num_cells`-seeded total would produce)."""
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(
        json.dumps({"modules": _YOSYS_033_HIER_MODULES}), encoding="utf-8"
    )

    result = synthesize._read_stats(str(stats_path), "top")

    assert result["num_cells"] == 3
    assert result["num_cells_by_type"] == {"$_NAND_": 2, "$_NOT_": 1}


#: A second live Yosys 0.33 capture from the same session: a 4-level design
#: (`top2` -> {`upper`, `mid`, `leaf`}, `upper` -> two `mid`, `mid` -> three
#: `leaf` + one direct `$_XOR_`) that exercises every hard case at once --
#: depth past two levels, a multiply-instantiated submodule, the same
#: submodule reachable by two different paths, and a parent holding both
#: direct cells and submodule instances. The pre-#821 pass-through reported
#: `num_cells: 3` with `{"leaf": 1, "mid": 1, "upper": 1}`.
_YOSYS_033_DEEP_MODULES = {
    "\\leaf": {"num_cells": 1, "num_cells_by_type": {"$_NAND_": 1}},
    "\\mid": {"num_cells": 4, "num_cells_by_type": {"$_XOR_": 1, "leaf": 3}},
    "\\upper": {"num_cells": 2, "num_cells_by_type": {"mid": 2}},
    "\\top2": {
        "num_cells": 3,
        "num_cells_by_type": {"leaf": 1, "mid": 1, "upper": 1},
    },
}


def test_read_stats_deep_multiply_instantiated_hierarchy(tmp_path):
    """Live 4-level capture: 3 `mid` instances x (1 `$_XOR_` + 3 `$_NAND_`)
    plus one directly-instantiated `leaf` = 10 `$_NAND_` + 3 `$_XOR_`, which
    is exactly what the same capture's own `design` block reports."""
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(
        json.dumps({"modules": _YOSYS_033_DEEP_MODULES}), encoding="utf-8"
    )

    result = synthesize._read_stats(str(stats_path), "top2")

    assert result["num_cells"] == 13
    assert result["num_cells_by_type"] == {"$_NAND_": 10, "$_XOR_": 3}


def test_read_stats_design_fallback_drops_submodule_pseudo_entries(tmp_path):
    """Defensive `data["design"]` fallback (top module absent from
    `modules`): `design.num_cells` is already the recursive total and is kept
    as-is, but the submodule-name pseudo entries Yosys mixes into
    `design.num_cells_by_type` must not be reported as cell types."""
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(
        json.dumps(
            {
                "modules": {"\\adder16": _ADDER16_MODULE_STATS},
                "design": {
                    "num_cells": 57,
                    "area": 999.0,
                    "num_cells_by_type": {
                        "sky130_fd_sc_hd__inv_1": 3,
                        "sky130_fd_sc_hd__xor2_1": 32,
                        "sky130_fd_sc_hd__a211o_1": 22,
                        "adder16": 1,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = synthesize._read_stats(str(stats_path), "renamed_top")

    assert result["num_cells"] == 57
    assert result["num_cells_by_type"] == {
        "sky130_fd_sc_hd__inv_1": 3,
        "sky130_fd_sc_hd__xor2_1": 32,
        "sky130_fd_sc_hd__a211o_1": 22,
    }


def test_read_stats_cyclic_modules_graph_terminates(tmp_path):
    """A malformed/adversarial `modules` graph where two modules instantiate
    each other must not recurse forever -- a module already on the recursion
    path is treated as a leaf."""
    stats_path = tmp_path / "stats.json"
    stats_path.write_text(
        json.dumps(
            {
                "modules": {
                    "\\a": {
                        "num_cells": 1,
                        "num_cells_by_type": {"sky130_fd_sc_hd__inv_1": 1, "b": 1},
                    },
                    "\\b": {
                        "num_cells": 1,
                        "num_cells_by_type": {"sky130_fd_sc_hd__buf_1": 1, "a": 1},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = synthesize._read_stats(str(stats_path), "a")

    assert result["num_cells_by_type"]["sky130_fd_sc_hd__inv_1"] == 1
    assert result["num_cells_by_type"]["sky130_fd_sc_hd__buf_1"] == 1


# --------------------------------------------------------------------------- #
# ABC delay target / cell exclusion / `stime -p` timing (issue #807)
# --------------------------------------------------------------------------- #


def test_generated_script_passes_constr_and_dont_use(tmp_path, monkeypatch):
    """The default (no `clock_period_ns`) run passes `-constr` -- which is
    what unlocks ABC's `buffer`/`upsize`/`dnsize` sizing steps and its
    `stime -p` report -- plus exactly one `-dont_use` per exclusion-table
    entry for the resolved library, and **no** `-D`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)
    abc_line = _script_abc_line(report["script_path"])

    constr_path = os.path.join(os.path.dirname(report["script_path"]), "gcd_abc.constr")
    assert f"-constr {constr_path}" in abc_line
    assert " -D " not in abc_line

    globs = synthesize._ABC_DONT_USE_GLOBS["sky130_fd_sc_hd"]
    assert globs, "the sky130 exclusion table entry must not be empty"
    assert abc_line.count("-dont_use ") == len(globs)
    for glob in globs:
        assert f"-dont_use {glob}" in abc_line


def test_generated_script_delay_target_from_clock_period_ns(tmp_path, monkeypatch):
    """`constraints.clock_period_ns` becomes ABC's own `-D <picoseconds>`
    delay target -- the field `docs/cli/synthesize.md` used to document as
    "accepted but not consumed"."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 2.5}),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert " -D 2500 " in _script_abc_line(report["script_path"]) + " "
    assert report["timing"]["delay_target_ps"] == 2500


@pytest.mark.parametrize("clock_period_ns", ["fast", True, [1]])
def test_clock_period_ns_must_be_a_number(tmp_path, monkeypatch, clock_period_ns):
    _isolate_pdk(monkeypatch, tmp_path)
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": clock_period_ns}),
    )
    with pytest.raises(SynthesizeError, match="clock_period_ns must be a positive"):
        run_synthesize(request_path)


@pytest.mark.parametrize("clock_period_ns", [0, -1.5])
def test_clock_period_ns_must_be_positive(tmp_path, monkeypatch, clock_period_ns):
    _isolate_pdk(monkeypatch, tmp_path)
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": clock_period_ns}),
    )
    with pytest.raises(SynthesizeError, match="greater than zero"):
        run_synthesize(request_path)


def test_abc_constr_file_is_written_from_the_table(tmp_path, monkeypatch):
    """The `<top>_abc.constr` artifact is the exact two-line file
    `help abc`/ORFS both use, built from this library's own table entry --
    never a hardcoded cell name."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    constr_path = os.path.join(os.path.dirname(report["script_path"]), "gcd_abc.constr")
    driving_cell, load_ff = synthesize._ABC_CONSTR_INPUTS["sky130_fd_sc_hd"]
    with open(constr_path, encoding="utf-8") as handle:
        assert (
            handle.read() == f"set_driving_cell {driving_cell}\nset_load {load_ff:g}\n"
        )


def test_dont_use_omitted_when_abc_pass_does_not_support_it(tmp_path, monkeypatch):
    """Ubuntu 24.04's Yosys 0.33 has no `abc -dont_use` at all (verified
    live, issue #807) and errors on an unknown option -- so the exclusion
    list degrades away on such builds instead of failing the run, the same
    posture `sequential_area_um2` already takes toward them (#560).
    `-constr`/`-D`/`stime` are unaffected: 0.33 documents all three."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch, version="0.33", supports_dont_use=False)

    report = run_synthesize(request_path)
    abc_line = _script_abc_line(report["script_path"])

    assert "-dont_use" not in abc_line
    assert "-constr " in abc_line
    assert report["timing"]["critical_path_ps"] == 2485.93


def test_cell_library_without_table_entries_keeps_the_pre_807_script(
    tmp_path, monkeypatch
):
    """A `cell_library` in neither new table is never given a guessed
    driving cell or exclusion list: the emitted `abc` line stays exactly
    what it was before issue #807, and `timing` stays `null`."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "acme_sc_hd", "corner": "tt_025C_1v80"}),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)
    abc_line = _script_abc_line(report["script_path"])

    assert abc_line.startswith("abc -liberty ")
    assert "-constr" not in abc_line
    assert "-dont_use" not in abc_line
    assert report["timing"] is None
    assert not os.path.isfile(
        os.path.join(os.path.dirname(report["script_path"]), "gcd_abc.constr")
    )


# --------------------------------------------------------------------------- #
# Constant-tie mapping (`hilomap`, issue #854)
# --------------------------------------------------------------------------- #


def _script_lines(script_path: str) -> list[str]:
    with open(script_path, encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


def _script_hilomap_line(script_path: str) -> str | None:
    for line in _script_lines(script_path):
        if line.startswith("hilomap "):
            return line
    return None


def test_generated_script_maps_constants_to_tie_cells(tmp_path, monkeypatch):
    """`sky130_fd_sc_hd` has a tie-cell table entry, so the generated script
    carries the `hilomap` pass that replaces every bare `1'b0`/`1'b1`
    constant driver with a real tie cell -- ORFS's own
    `TIEHI_CELL_AND_PORT`/`TIELO_CELL_AND_PORT` values for that platform
    (issue #854)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert _script_hilomap_line(report["script_path"]) == (
        "hilomap -hicell sky130_fd_sc_hd__conb_1 HI -locell sky130_fd_sc_hd__conb_1 LO"
    )


def test_hilomap_runs_after_mapping_and_before_stat(tmp_path, monkeypatch):
    """Ordering is load-bearing: `hilomap` can only see the constants ABC
    left behind, so it must follow `abc`/`clean`, and the tie cells it
    inserts are real instances, so it must precede `stat` (which populates
    `instance_count`/`area_um2`) and `write_verilog`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)
    lines = _script_lines(report["script_path"])
    index = {
        "clean": lines.index("clean"),
        "hilomap": next(
            i for i, line in enumerate(lines) if line.startswith("hilomap")
        ),
        "stat": next(i for i, line in enumerate(lines) if " stat -liberty " in line),
        "write_verilog": next(
            i for i, line in enumerate(lines) if line.startswith("write_verilog ")
        ),
    }

    assert index["clean"] < index["hilomap"] < index["stat"] < index["write_verilog"]


def test_generated_script_maps_constants_to_tie_cells_gf180mcu(tmp_path, monkeypatch):
    """The second supported library gets its **own** verified tie cells
    (`__tieh`/`__tiel`, two distinct cells) -- never sky130's single
    dual-output `conb_1` carried over by analogy."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuD",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        corners=(("tt_025C_5v00", 1.0, 25.0, 5.0),),
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={
                "cell_library": "gf180mcu_fd_sc_mcu9t5v0",
                "corner": "tt_025C_5v00",
            }
        ),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert _script_hilomap_line(report["script_path"]) == (
        "hilomap -hicell gf180mcu_fd_sc_mcu9t5v0__tieh Z "
        "-locell gf180mcu_fd_sc_mcu9t5v0__tiel ZN"
    )


def test_cell_library_without_tie_cell_entry_emits_no_hilomap(tmp_path, monkeypatch):
    """A `cell_library` with no `_TIE_CELLS` entry is never given a guessed
    tie cell: the `hilomap` pass is omitted entirely rather than the run
    failing on a cell name that does not exist in that library -- the same
    graceful degradation `_ABC_CONSTR_INPUTS`/`_ABC_DONT_USE_GLOBS` already
    apply."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "acmeA", cell_library="acme_sc_hd")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(pdk={"cell_library": "acme_sc_hd", "corner": "tt_025C_1v80"}),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert _script_hilomap_line(report["script_path"]) is None


def test_tie_cell_table_entries_are_hi_then_lo(tmp_path):
    """Each `_TIE_CELLS` entry is `((hi_cell, hi_port), (lo_cell, lo_port))`
    -- guards against an entry being written the other way round, which
    `hilomap` would happily accept while inverting every constant in the
    design."""
    for cell_library, entry in synthesize._TIE_CELLS.items():
        (hi_cell, hi_port), (lo_cell, lo_port) = entry
        for cell in (hi_cell, lo_cell):
            assert cell.startswith(f"{cell_library}__"), cell
        assert hi_port and lo_port
        assert (hi_cell, hi_port) != (lo_cell, lo_port)


def test_timing_reports_the_maximum_over_every_stime_report(tmp_path, monkeypatch):
    """Yosys invokes ABC once per combinational region, so a multi-region
    run prints several `stime` summaries -- the reported critical path is
    the largest of them, not whichever was mapped last."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(
        monkeypatch,
        abc_log_lines=(
            _ABC_STIME_LINE
            + "\n"
            + _ABC_STIME_LINE.replace("2485.93 ps", "3910.55 ps")
            + "\n"
            + _ABC_STIME_LINE.replace("2485.93 ps", "1204.10 ps")
        ),
    )

    report = run_synthesize(request_path)

    assert report["timing"]["critical_path_ps"] == 3910.55


def test_timing_echoes_a_non_none_wire_load(tmp_path, monkeypatch):
    """`wire_load` is ABC's own `WireLoad = "..."` echo, normalised to
    `None` only for its literal `"none"` -- the pre-layout caveat made
    machine-readable rather than assumed."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(
        monkeypatch,
        abc_log_lines=_ABC_STIME_LINE.replace('WireLoad = "none"', 'WireLoad = "5K"'),
    )

    report = run_synthesize(request_path)

    assert report["timing"]["wire_load"] == "5K"


def test_timing_is_null_when_abc_printed_no_stime_line(tmp_path, monkeypatch):
    """No invented number when ABC's own summary is absent (e.g. a build
    whose `stime -p` output shape this parser does not recognise)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch, abc_log_lines="ABC: nothing to report here\n")

    report = run_synthesize(request_path)

    assert report["timing"] is None


# --------------------------------------------------------------------------- #
# Non-sky130 cell library + `--pdk`/`--pdk-root` (issue #629)
# --------------------------------------------------------------------------- #


def test_resolve_liberty_pdk_variant_pins_a_specific_install(tmp_path, monkeypatch):
    """`_resolve_liberty`'s `variant`/`root` kwargs (the CLI's own
    `--pdk`/`--pdk-root` flags, threaded through `run_synthesize`) pin a
    specific installed variant, beating both `$PDK` and `find_pdk()`'s own
    alphabetical-first default -- mirroring `klt extract`'s identical
    `--pdk` behavior."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    _make_pdk_install(install_root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    monkeypatch.setenv("PDK", "sky130A")

    liberty_path, corner, info = synthesize._resolve_liberty(
        "sky130_fd_sc_hd", None, variant="sky130B"
    )
    assert info["variant"] == "sky130B"
    assert corner == "tt_025C_1v80"
    assert "sky130B" in liberty_path


def test_run_synthesize_stubbed_success_gf180mcu(tmp_path, monkeypatch):
    """A second, non-sky130 cell library resolves and synthesizes the same
    way sky130 does -- `find_pdk()`/`list_cell_libraries()` were never
    sky130-specific, only every fixture above happened to only exercise
    sky130 (issue #629)."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(
        install_root,
        "gf180mcuC",
        cell_library="gf180mcu_fd_sc_mcu9t5v0",
        corners=(("tt_025C_1v80", 1.0, 25.0, 1.8),),
    )
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            pdk={"cell_library": "gf180mcu_fd_sc_mcu9t5v0", "corner": "tt_025C_1v80"}
        ),
    )
    _stub_yosys_success(monkeypatch)

    report = run_synthesize(request_path)

    assert report["status"] == "ok"
    assert report["provenance"]["pdk"]["name"] == "gf180mcuC"
    assert (
        report["provenance"]["deck"]["name"] == "gf180mcu_fd_sc_mcu9t5v0__tt_025C_1v80"
    )

    # Issue #807: this library has an ABC `-constr` table entry (so it gets
    # sizing + a `timing` number) but deliberately **no** `-dont_use` entry
    # -- ORFS's own `DONT_USE_CELLS = *_1` for this platform is a P&R
    # congestion policy, measured here to cost +31% area at synthesis time.
    # See `_ABC_DONT_USE_GLOBS`'s own docstring.
    abc_line = _script_abc_line(report["script_path"])
    assert "-constr " in abc_line
    assert "-dont_use" not in abc_line
    assert "gf180mcu_fd_sc_mcu9t5v0" not in synthesize._ABC_DONT_USE_GLOBS
    assert report["timing"]["source"] == "abc_stime"


def test_cli_pdk_flag_pins_variant(tmp_path, monkeypatch, capsys):
    """`--pdk` (issue #629) selects a specific installed variant, beating
    `$PDK` -- mirroring `klt extract`'s identical flag."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    _make_pdk_install(install_root, "sky130B")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    monkeypatch.setenv("PDK", "sky130A")
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_yosys_success(monkeypatch)

    exit_code = main(
        ["synthesize", request_path, "--pdk", "sky130B", "--format", "json"]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provenance"]["pdk"]["name"] == "sky130B"


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
# `verify_equivalence` gate (issue #808, Phase 1 of Epic #704): wires `klt
# equiv` (#726) in as `klt synthesize`'s acceptance gate. Stubbed here (both
# `synthesize.subprocess.run` and `synthesize.run_equiv` itself replaced by
# fakes) so this tier runs fast and deterministically in every environment,
# with no real `yosys`/`iverilog` binary required -- the real, unmocked
# end-to-end demonstration (a genuine pass and a genuine seeded-mismatch
# hard failure through real Yosys/Icarus) lives in
# `tests/test_synthesize_equiv_gate.py`.
# --------------------------------------------------------------------------- #

_FAKE_EQUIV_EQUIVALENT = {
    "status": "equivalent",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "counterexample": None,
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": "/fake/.klt/equiv/equiv_netlist.v",
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}

_FAKE_EQUIV_COUNTEREXAMPLE = {
    "status": "counterexample",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "counterexample": {
        "diverging_outputs": ["result"],
        "confirmed_by_simulation": True,
    },
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": "/fake/.klt/equiv/equiv_netlist.v",
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}

_FAKE_EQUIV_INCONCLUSIVE = {
    "status": "inconclusive",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 60.02,
    "counterexample": None,
    "diagnostics": [
        {
            "severity": "error",
            "code": "process_timeout",
            "message": "yosys did not complete within 60.0s",
        }
    ],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": None,
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}


def test_verify_equivalence_default_off_never_calls_equiv(tmp_path, monkeypatch):
    """`verify_equivalence` defaults to `False` -- additive/opt-in, no
    behaviour change for every pre-existing caller. `report["equivalence"]`
    is explicitly `None` (always present, per the module's `timing: null`
    precedent), and `run_equiv` is never even called."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    calls = []
    monkeypatch.setattr(synthesize, "run_equiv", lambda *a, **k: calls.append(1))

    report = run_synthesize(request_path)

    assert report["equivalence"] is None
    assert calls == []


def test_verify_equivalence_true_attaches_report_on_pass(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    captured_request_paths = []

    def fake_run_equiv(request, *, timeout_s=None):
        captured_request_paths.append(request)
        return _FAKE_EQUIV_EQUIVALENT

    monkeypatch.setattr(synthesize, "run_equiv", fake_run_equiv)

    report = run_synthesize(request_path, verify_equivalence=True)

    assert report["status"] == "ok"
    assert report["equivalence"]["status"] == "equivalent"
    assert report["equivalence"]["engine"] == "yosys"
    assert (
        report["equivalence"]["artifacts"]["log_path"] == "/fake/.klt/equiv/equiv.log"
    )

    # `run_equiv` is called with a **request file path** (never an inline
    # JSON string -- see `_verify_synthesis_equivalence`'s own docs) so its
    # own `.klt/equiv/` artifacts land next to the synthesize request, not
    # the process's cwd. `gold` = the same source RTL synthesis just read,
    # `gate` = the netlist synthesis just produced, `gate.liberty` set to
    # the same resolved liberty synthesis used.
    assert len(captured_request_paths) == 1
    equiv_request_path = captured_request_paths[0]
    assert os.path.isabs(equiv_request_path)
    assert os.path.dirname(equiv_request_path) == os.path.join(
        os.path.dirname(request_path), ".klt", "synthesize"
    )
    with open(equiv_request_path, encoding="utf-8") as handle:
        equiv_request = json.load(handle)
    assert equiv_request["gold"]["top"] == "gcd"
    assert equiv_request["gold"]["sources"][0].endswith("gcd.v")
    assert equiv_request["gate"]["top"] == "gcd"
    assert equiv_request["gate"]["sources"] == [report["netlist_path"]]
    assert equiv_request["gate"]["liberty"].endswith(
        "sky130_fd_sc_hd__tt_025C_1v80.lib"
    )


def test_verify_equivalence_counterexample_is_hard_failure_not_warning(
    tmp_path, monkeypatch
):
    """Acceptance criterion #1: a non-equivalent result is a hard failure
    (`SynthesizeError`, exit 1 via the CLI), never a silent warning folded
    into a successful `status: "ok"` response."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize,
        "run_equiv",
        lambda request, *, timeout_s=None: _FAKE_EQUIV_COUNTEREXAMPLE,
    )

    with pytest.raises(SynthesizeError, match="NOT equivalent") as exc_info:
        run_synthesize(request_path, verify_equivalence=True)
    assert "result" in str(exc_info.value)
    assert "confirmed_by_simulation=True" in str(exc_info.value)


def test_verify_equivalence_inconclusive_is_hard_failure_never_treated_as_pass(
    tmp_path, monkeypatch
):
    """A solver/process timeout (`"inconclusive"`) must never be treated as
    a pass -- mirrors `klt equiv`'s own "timeout is never equivalent"
    discipline, one level up."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize,
        "run_equiv",
        lambda request, *, timeout_s=None: _FAKE_EQUIV_INCONCLUSIVE,
    )

    with pytest.raises(SynthesizeError, match="did not reach a verdict"):
        run_synthesize(request_path, verify_equivalence=True)


def test_verify_equivalence_wraps_equiv_error(tmp_path, monkeypatch):
    """An `EquivError` (e.g. a sequential design -- outside `klt equiv`'s
    combinational-only MVP scope) is not attempted-to-be-recovered from --
    it surfaces as a `SynthesizeError`, still a hard failure."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    def fake_run_equiv(request, *, timeout_s=None):
        raise EquivError("combinational-only MVP: gold contains sequential elements")

    monkeypatch.setattr(synthesize, "run_equiv", fake_run_equiv)

    with pytest.raises(SynthesizeError, match="could not be completed"):
        run_synthesize(request_path, verify_equivalence=True)


def test_verify_equivalence_timeout_s_passed_through(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)

    captured_timeouts = []

    def fake_run_equiv(request, *, timeout_s=None):
        captured_timeouts.append(timeout_s)
        return _FAKE_EQUIV_EQUIVALENT

    monkeypatch.setattr(synthesize, "run_equiv", fake_run_equiv)

    run_synthesize(request_path, verify_equivalence=True, equiv_timeout_s=12.5)

    assert captured_timeouts == [12.5]


def test_cli_verify_equivalence_flag_wires_through_and_exits_zero(
    tmp_path, monkeypatch, capsys
):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize,
        "run_equiv",
        lambda request, *, timeout_s=None: _FAKE_EQUIV_EQUIVALENT,
    )

    exit_code = main(
        ["synthesize", request_path, "--verify-equivalence", "--format", "json"]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equivalence"]["status"] == "equivalent"


def test_cli_verify_equivalence_flag_hard_fails_exits_one(
    tmp_path, monkeypatch, capsys
):
    """A non-equivalent verdict exits `1` through the CLI -- not a distinct
    "equivalence failed" exit code (synthesis has no pass/fail concept of
    its own beyond "did a netlist come out that this run trusts", see
    `cli/synthesize_cmd.py`'s exit-code docstring) and not `0`."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize,
        "run_equiv",
        lambda request, *, timeout_s=None: _FAKE_EQUIV_COUNTEREXAMPLE,
    )

    exit_code = main(
        ["synthesize", request_path, "--verify-equivalence", "--format", "json"]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "synthesize"
    assert "NOT equivalent" in err["error"]["message"]


def test_cli_verify_equivalence_not_given_defaults_false(tmp_path, monkeypatch, capsys):
    """Omitting `--verify-equivalence` never calls `klt equiv` -- backward
    compatible with every pre-existing `klt synthesize` invocation."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    calls = []
    monkeypatch.setattr(synthesize, "run_equiv", lambda *a, **k: calls.append(1))

    exit_code = main(["synthesize", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equivalence"] is None
    assert calls == []


# --------------------------------------------------------------------------- #
# Integration: real Yosys + a real, host-resolved sky130 PDK
# (skipped when either is unavailable -- never required for CI, see this
# module's own docstring)
# --------------------------------------------------------------------------- #

HAVE_YOSYS = shutil.which("yosys") is not None

#: The Yosys build `docs/cli/synthesize.md`'s worked-example numbers were
#: measured on. Issue #967: originally pinned to "0.68" (matching the
#: prebuilt oss-cad-suite `0.68+48` build issue #822 measured on), but CI's
#: own `scripts/install-yosys.sh` pins Yosys **0.67** (an exact, checksum-
#: verified upstream release tag) -- a mismatch that meant this gate's
#: exact-number branch never actually ran in CI (silently skipped every
#: run since #822). A real bisect (issue #967) found `synthesize.py` was
#: never the cause of the drift: even the exact commit that introduced the
#: `3238.1056` golden reproduces `3126.7488` today against *any* real,
#: from-source Yosys build (0.67 built via `scripts/install-yosys.sh`, and
#: 0.68+post via Homebrew both agree) -- only the original oss-cad-suite
#: prebuilt binary distribution produced the stale `3238.1056` figure.
#: Re-pinned to "0.67" (CI's own reproducible, checksum-verified build) so
#: this exact-number check actually runs in CI on every push, instead of
#: to an arbitrary contributor's local Yosys major version.
_WORKED_EXAMPLE_YOSYS = "0.67"


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
    sky130 PDK install.

    The build-independent assertions -- **zero** excluded (`lpflow_*`/
    `probe*`) cells in `instance_counts_by_type`, and a populated, self-
    labelling `timing` object -- are the issue #807 acceptance criteria and
    always run. The exact instance/area numbers are asserted only on the
    Yosys build `docs/cli/synthesize.md`'s worked example was measured on,
    because Yosys's own mapping result legitimately moves between releases
    (measured: the pre-#807 flow maps this design to 335 cells on 0.68 and
    326 on Ubuntu 24.04's 0.33)."""
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    request_path = _setup_success_env_real(tmp_path)

    report = run_synthesize(request_path)

    assert report["status"] == "ok"
    assert report["engine"] == "yosys"
    assert report["instance_count"] > 0
    assert report["instance_counts_by_type"]["sky130_fd_sc_hd__dfrtp_1"] == 50

    # Issue #807: no power-domain isolation or test-probe cell survives the
    # `-dont_use` list (the pre-#807 flow mapped 19 of them into this exact
    # design -- 16 `lpflow_isobufsrc_1` + 3 `lpflow_inputiso1p_1`).
    if synthesize._abc_supports_dont_use():
        excluded = {
            cell: count
            for cell, count in report["instance_counts_by_type"].items()
            if "lpflow" in cell or "probe" in cell
        }
        assert excluded == {}

    # Issue #807: ABC's own `stime -p` number, labelled as the pre-layout,
    # wire-free estimate it is -- never `null` for a library with a
    # `-constr` table entry.
    timing = report["timing"]
    assert timing["source"] == "abc_stime"
    assert timing["wire_load"] is None
    assert timing["critical_path_ps"] > 0
    assert timing["delay_target_ps"] is None  # no `constraints` in this request

    if (report["engine_version"] or "").startswith(_WORKED_EXAMPLE_YOSYS):
        assert report["instance_count"] == 347
        assert report["area_um2"] == pytest.approx(3126.7488)
        assert report["sequential_area_um2"] == pytest.approx(1251.2)
    else:
        # Issue #967: this golden previously drifted silently -- CI pinned
        # Yosys 0.67 while the gate checked "0.68", so the exact-number
        # branch above never ran in CI from the day it was introduced
        # (#822) until this fix. Now that the gate matches CI's own pinned,
        # from-source build (see the constant's docstring), this branch
        # should be unreachable in CI; warn loudly (pytest always surfaces
        # warnings in its summary, unlike a bare `print`) rather than
        # silently skip if it ever is again -- e.g. after a future Yosys
        # version bump this test's gate is not updated for.
        warnings.warn(
            "test_integration_real_yosys_gcd_worked_example: resolved "
            f"engine_version={report['engine_version']!r} does not match "
            f"_WORKED_EXAMPLE_YOSYS={_WORKED_EXAMPLE_YOSYS!r} -- the "
            "instance_count/area_um2/sequential_area_um2 golden assertions "
            "were skipped. If this is CI, the pinned Yosys build "
            "(scripts/install-yosys.sh) or this gate has drifted out of "
            "sync -- see issue #967.",
            stacklevel=1,
        )

    assert os.path.isfile(report["netlist_path"])
    assert os.path.isfile(report["script_path"])
    assert os.path.isfile(
        os.path.join(os.path.dirname(report["script_path"]), "gcd_abc.constr")
    )

    # `write_verilog -noattr`'s own output is a real, non-trivial netlist --
    # sanity-check it names the top module and at least one mapped cell.
    with open(report["netlist_path"], encoding="utf-8") as handle:
        netlist_text = handle.read()
    assert "module gcd" in netlist_text
    assert "sky130_fd_sc_hd__dfrtp_1" in netlist_text


def _setup_success_env_real(tmp_path: Path) -> str:
    _write(tmp_path / "gcd.v", _GCD_RTL)
    return _write_request(tmp_path / "request.json", _base_request())


#: `klt_statime_native` (issue #925, Epic #704 Phase 3) is an optional Rust
#: extension -- same posture as `klt_mom_native`/`klt_congestion_native`/
#: `klt_yield_native` (see `tests/test_mom.py`'s identical `importorskip`).
#: Its absence must never fail this integration test; it means `sta` is
#: `None` (asserted in the always-run tier above) and the extra checks below
#: are skipped with a clear reason.
_HAVE_STATIME_NATIVE = importlib.util.find_spec("klt_statime_native") is not None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None,
    reason="no real sky130_fd_sc_hd liberty resolves via list_pdks() on this machine",
)
@pytest.mark.skipif(
    not _HAVE_STATIME_NATIVE,
    reason=(
        "klt_statime_native is not built -- run `maturin develop --release` "
        "in native/statime/ (see docs/cli/synthesize.md's 'sta' section)"
    ),
)
def test_integration_real_yosys_gcd_sta_field(tmp_path, monkeypatch):
    """Issue #925 (Epic #704 Phase 3), end to end against a real Yosys, a
    real sky130 PDK install, and the built `klt_statime_native` extension:
    `klt synthesize`'s response carries a populated `sta` critical-path
    report -- the same worst register-to-register/port-to-port delay this
    engine's own spike (`native/statime/README.md`) reported for the
    identical `gcd` corpus design, now reached through the integrated
    `run_synthesize` path rather than the standalone `klt-statime
    critical-path` binary."""
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    request_path = _setup_success_env_real(tmp_path)

    report = run_synthesize(request_path)

    assert report["status"] == "ok"

    sta = report["sta"]
    assert sta is not None, "sta must be populated when klt_statime_native is built"
    assert sta["source"] == "klt_statime_native"
    assert sta["input_transition_ns"] == pytest.approx(0.05)
    assert sta["output_load_pf"] == pytest.approx(0.03)
    assert sta["top"] == "gcd"
    assert sta["num_cells"] > 0

    worst = sta["worst_path"]
    assert worst["delay_ns"] > 0
    assert worst["startpoint"]
    assert worst["endpoint"]
    assert worst["hops"], "the limiting path's cells must be reported"

    # `gcd` is a sequential design (issue #809's own corpus) -- a
    # register-to-register path must exist.
    r2r = sta["worst_reg_to_reg_path"]
    assert r2r is not None
    assert r2r["delay_ns"] > 0
    assert r2r["hops"]

    # `timing` (ABC's pre-layout, combinational-cone-only estimate) is
    # unaffected by this addition -- both fields coexist, neither replaces
    # the other (issue #925 AC: additive, not a replacement).
    assert report["timing"] is not None


#: RTL whose synthesized netlist provably needs constant ties: `q[5]` is a
#: constant-0 output port and the `q[0]` flop's `D` pin is a constant 1.
#: Measured live (Yosys 0.33 + real sky130 liberty, issue #854): without the
#: `hilomap` pass the mapped netlist carries a bare `assign q[5] = 1'h0;`
#: and a bare `.D(1'h1)` -- exactly the shape OpenSTA's Verilog reader turns
#: into the `zero_`/`one_` nets TritonRoute rejects with `DRT-0305`.
_TIE_CONST_RTL = """\
module tie_const (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [3:0] d,
    output reg  [5:0] q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= 6'b0;
        else        q <= {1'b0, d, 1'b1};
    end
endmodule
"""

#: Matches any bare Verilog constant literal (`1'h0`, `2'b10`, `6'd3`, ...)
#: in a mapped netlist -- what issue #854's fix must leave none of.
_BARE_CONSTANT_RE = re.compile(r"\d+'[bdho][0-9a-fxzA-FXZ_]+")


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None,
    reason="no real sky130_fd_sc_hd liberty resolves via list_pdks() on this machine",
)
def test_integration_real_yosys_constant_ties_become_tie_cells(tmp_path, monkeypatch):
    """Issue #854, end to end against a real Yosys and a real sky130
    install: a design that needs constant ties synthesizes to a netlist with
    **zero** bare constant literals, every one of them replaced by a real
    `sky130_fd_sc_hd__conb_1` tie-cell instance.

    This is the root-cause check for `DRT-0305` (`Net zero_ of signal type
    GROUND is not routable by TritonRoute`): OpenSTA's Verilog reader
    synthesizes a `zero_`/`one_` net for every bare constant it reads, and
    OpenROAD types those nets GROUND/POWER, which `detailed_route` refuses.
    No bare constant in the netlist means no such net exists to reject."""
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    _write(tmp_path / "tie_const.v", _TIE_CONST_RTL)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(sources=["tie_const.v"], hdl_toplevel="tie_const"),
    )

    report = run_synthesize(request_path)

    assert report["status"] == "ok"
    with open(report["netlist_path"], encoding="utf-8") as handle:
        netlist_text = handle.read()

    body = "\n".join(
        line for line in netlist_text.splitlines() if not line.lstrip().startswith("//")
    )
    assert _BARE_CONSTANT_RE.search(body) is None, (
        "mapped netlist still carries a bare constant literal -- OpenROAD "
        "would build a `zero_`/`one_` power/ground net from it and "
        "`detailed_route` would abort with DRT-0305"
    )
    assert report["instance_counts_by_type"]["sky130_fd_sc_hd__conb_1"] >= 1
    assert "sky130_fd_sc_hd__conb_1" in netlist_text


# --------------------------------------------------------------------------- #
# `restructure_timing` gate (issue #926, Epic #704 Phase 3): wires
# `klayout_tools.restructure.restructure_for_timing`'s bounded cell-resizing
# loop in as an opt-in stage after `sta`. `restructure_for_timing` itself is
# fully covered hermetically in `tests/test_restructure.py`; these tests only
# exercise `synthesize.py`'s own wiring -- the hard-failure guards, the
# `klt equiv` gate on an applied resize, and the response shape -- via a
# stubbed `synthesize.restructure_for_timing`.
# --------------------------------------------------------------------------- #

_FAKE_STA_RESULT = {
    "source": "klt_statime_native",
    "input_transition_ns": 0.05,
    "output_load_pf": 0.03,
    "top": "gcd",
    "num_cells": 353,
    "num_nets": 388,
    "worst_path": {
        "startpoint": "_640_/Q",
        "startpoint_kind": "flip-flop (rising edge)",
        "endpoint": "_035_",
        "endpoint_kind": "internal net",
        "delay_ns": 4.4642972825718,
        "hops": [],
    },
    "worst_reg_to_reg_path": None,
}

_FAKE_RESTRUCTURE_NO_VIOLATION = {
    "target_period_ns": 5.0,
    "max_iterations": 8,
    "initial_worst_path_delay_ns": 4.4642972825718,
    "final_worst_path_delay_ns": 4.4642972825718,
    "converged": True,
    "iterations_used": 0,
    "gave_up_reason": None,
    "resizes_applied": [],
    "restructured_netlist_path": None,
}


def _fake_restructure_with_resize(output_netlist_path: str) -> dict:
    with open(output_netlist_path, "w", encoding="utf-8") as handle:
        handle.write("// fake restructured netlist\n")
    return {
        "target_period_ns": 3.0,
        "max_iterations": 8,
        "initial_worst_path_delay_ns": 4.4642972825718,
        "final_worst_path_delay_ns": 2.9,
        "converged": True,
        "iterations_used": 1,
        "gave_up_reason": None,
        "resizes_applied": [
            {
                "instance": "_377_",
                "from_cell": "sky130_fd_sc_hd__xnor2_1",
                "to_cell": "sky130_fd_sc_hd__xnor2_2",
            }
        ],
        "restructured_netlist_path": output_netlist_path,
    }


def test_restructure_timing_default_off_never_calls_restructure(tmp_path, monkeypatch):
    """`restructure_timing` defaults to `False` -- additive/opt-in, no
    behaviour change for every pre-existing caller. `report["restructuring"]`
    is explicitly `None`, and `restructure_for_timing` is never even
    called."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    calls = []
    monkeypatch.setattr(
        synthesize, "restructure_for_timing", lambda *a, **k: calls.append(1)
    )

    report = run_synthesize(request_path)

    assert report["restructuring"] is None
    assert calls == []


def test_restructure_timing_requires_clock_period_ns(tmp_path, monkeypatch):
    """The flag was explicitly requested but there is no target to
    restructure against -- a hard, actionable failure, never a silent
    no-op."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )

    with pytest.raises(
        SynthesizeError, match="requires request.constraints.clock_period_ns"
    ):
        run_synthesize(request_path, restructure_timing=True)


def test_restructure_timing_requires_working_sta_stage(tmp_path, monkeypatch):
    """`sta` degrades to `None` when the optional extension is missing or
    analysis fails; `restructure_timing` cannot proceed without it, and --
    since it was explicitly requested -- reports that clearly rather than
    silently doing nothing."""
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 3.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    # No `compute_critical_path` stub -- the fake netlist from
    # `_stub_yosys_success` is unanalyzable, so `sta` naturally stays `None`.

    with pytest.raises(SynthesizeError, match="requires a working sta stage"):
        run_synthesize(request_path, restructure_timing=True)


def test_restructure_timing_no_violation_skips_equiv_gate(tmp_path, monkeypatch):
    """When the restructuring loop applies no resize (already meets target),
    no `klt equiv` check is run at all -- there is nothing new to verify."""
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 5.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )
    monkeypatch.setattr(
        synthesize,
        "restructure_for_timing",
        lambda *a, **k: dict(_FAKE_RESTRUCTURE_NO_VIOLATION),
    )
    equiv_calls = []
    monkeypatch.setattr(synthesize, "run_equiv", lambda *a, **k: equiv_calls.append(1))

    report = run_synthesize(request_path, restructure_timing=True)

    assert report["restructuring"]["converged"] is True
    assert report["restructuring"]["resizes_applied"] == []
    assert report["restructuring"]["equivalence"] is None
    assert equiv_calls == []


def test_restructure_timing_applied_resize_is_verified_by_equiv(tmp_path, monkeypatch):
    """Acceptance criterion 3: any resize the loop actually applies is
    validated by `klt equiv` against the source RTL before the report is
    returned, using a distinct request file from `--verify-equivalence`'s
    own (so the two gates, if both requested in one run, never clobber each
    other's artifact)."""
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 3.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )

    def fake_restructure(
        netlist_path,
        liberty_path,
        top,
        target_period_ns,
        *,
        output_netlist_path,
        **kwargs,
    ):
        return _fake_restructure_with_resize(output_netlist_path)

    monkeypatch.setattr(synthesize, "restructure_for_timing", fake_restructure)

    captured_request_paths = []

    def fake_run_equiv(request, *, timeout_s=None):
        captured_request_paths.append(request)
        return _FAKE_EQUIV_EQUIVALENT

    monkeypatch.setattr(synthesize, "run_equiv", fake_run_equiv)

    report = run_synthesize(request_path, restructure_timing=True)

    assert report["restructuring"]["resizes_applied"] != []
    assert report["restructuring"]["equivalence"]["status"] == "equivalent"
    assert len(captured_request_paths) == 1
    assert (
        os.path.basename(captured_request_paths[0])
        == "equiv_request_gcd_restructured.json"
    )

    with open(captured_request_paths[0], encoding="utf-8") as handle:
        equiv_request = json.load(handle)
    assert equiv_request["gate"]["sources"] == [
        report["restructuring"]["restructured_netlist_path"]
    ]


def test_restructure_timing_applied_resize_non_equivalent_is_hard_failure(
    tmp_path, monkeypatch
):
    """A restructured netlist that `klt equiv` cannot prove equivalent to
    its source RTL must never ship -- 'never just re-timed at the cost of
    correctness' (acceptance criterion 3)."""
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 3.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )

    def fake_restructure(
        netlist_path,
        liberty_path,
        top,
        target_period_ns,
        *,
        output_netlist_path,
        **kwargs,
    ):
        return _fake_restructure_with_resize(output_netlist_path)

    monkeypatch.setattr(synthesize, "restructure_for_timing", fake_restructure)
    monkeypatch.setattr(
        synthesize,
        "run_equiv",
        lambda request, *, timeout_s=None: _FAKE_EQUIV_COUNTEREXAMPLE,
    )

    with pytest.raises(SynthesizeError, match="NOT equivalent"):
        run_synthesize(request_path, restructure_timing=True)


def test_restructure_timing_max_iterations_passed_through(tmp_path, monkeypatch):
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 3.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )

    captured_kwargs = {}

    def fake_restructure(
        netlist_path,
        liberty_path,
        top,
        target_period_ns,
        *,
        output_netlist_path,
        **kwargs,
    ):
        captured_kwargs.update(kwargs)
        return dict(_FAKE_RESTRUCTURE_NO_VIOLATION)

    monkeypatch.setattr(synthesize, "restructure_for_timing", fake_restructure)

    run_synthesize(request_path, restructure_timing=True, restructure_max_iterations=3)

    assert captured_kwargs == {"max_iterations": 3}


def test_cli_restructure_timing_flag_wires_through_and_exits_zero(
    tmp_path, monkeypatch, capsys
):
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(constraints={"clock_period_ns": 5.0}),
    )
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setattr(
        synthesize, "compute_critical_path", lambda *a, **k: _FAKE_STA_RESULT
    )
    monkeypatch.setattr(
        synthesize,
        "restructure_for_timing",
        lambda *a, **k: dict(_FAKE_RESTRUCTURE_NO_VIOLATION),
    )

    exit_code = main(
        ["synthesize", request_path, "--restructure-timing", "--format", "json"]
    )

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["restructuring"]["converged"] is True


def test_cli_restructure_timing_not_given_defaults_false(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    calls = []
    monkeypatch.setattr(
        synthesize, "restructure_for_timing", lambda *a, **k: calls.append(1)
    )

    exit_code = main(["synthesize", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["restructuring"] is None
    assert calls == []


# Sanity: `subprocess` really is the module this file's stubs patch (guards
# against a future refactor silently making the stubs a no-op).
def test_synthesize_uses_stdlib_subprocess():
    assert synthesize.subprocess is subprocess
