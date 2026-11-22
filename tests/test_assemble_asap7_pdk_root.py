"""End-to-end checks for the standalone ASAP7 root assembler."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import pdk
from klayout_tools.pdk_cells import resolve_liberty_for_cell_library

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "assemble-asap7-pdk-root.py"
CELL_DIR = "asap7sc7p5t_28"
PDK_DIR = "asap7_pdk_r1p7"
LAYER_MAP = "cdslib/asap7_TechLib_10/asap7_fromAPR.layermap"
GROUP_DATES = {
    "AO": "211120",
    "INVBUF": "220122",
    "OA": "211120",
    "SEQ": "220123",
    "SIMPLE": "211120",
}


def _put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def _git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def _repo(path):
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "sources"
    _put(root / "README", "source fixture")
    _repo(root)
    cells = root / CELL_DIR
    for suffix, vt in (("R", "RVT"), ("L", "LVT")):
        _put(cells / "LEF" / f"{CELL_DIR}_{suffix}_1x_220121a.lef", "lef")
        _put(cells / "GDS" / f"{CELL_DIR}_{suffix}_220121a.gds", "gds")
        for corner in ("TT", "SS"):
            for group, revision in GROUP_DATES.items():
                default = 4000 if group == "SIMPLE" else 320
                _put(
                    cells
                    / "LIB/NLDM"
                    / f"asap7sc7p5t_{group}_{vt}_{corner}_nldm_{revision}.lib",
                    f"""library (example) {{
  default_max_transition : {default};
  cell ({group}x1_{vt}) {{
    pin (Y) {{
      direction : output;
    }}
    pin (A) {{
      direction : input;
      max_transition : 100;
    }}
  }}
}}
""",
                )
    _put(cells / "techlef_misc/asap7_tech_1x_201209.lef", "tech lef")
    _put(root / PDK_DIR / LAYER_MAP, "M2 NET 20 0\n")
    _repo(cells)
    _repo(root / PDK_DIR)
    return root


def _run(source, output, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--asap7", str(source), "-o", str(output), *args],
        capture_output=True,
        text=True,
    )


def test_assembled_root_records_real_inputs_and_matches_named_views(source, tmp_path):
    root = tmp_path / "assembled"
    result = _run(source, root, "--vt", "RVT", "LVT", "--corner", "TT", "SS")
    assert result.returncode == 0, result.stderr
    info = pdk.find_pdk(variant="asap7", root=str(root))
    layer_map, resolution = pdk.resolve_klayout_layer_map(info)
    assert resolution == "exact"
    assert Path(layer_map).read_text() == "M2 NET 20 0\n"
    provenance = json.loads((root / "klt-pdk-sources.json").read_text())
    assert provenance["commits"] == {
        name: _git(source / name, "rev-parse", "HEAD")
        for name in (".", CELL_DIR, PDK_DIR)
    }
    for name, digest in provenance["outputs"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    for name, digest in provenance["inputs"].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
    assert not any(path.is_symlink() for path in root.rglob("*"))
    lib, corner, _ = resolve_liberty_for_cell_library(
        "asap7sc7p5t_lvt", "SS", ValueError, variant="asap7", root=str(root)
    )
    assert corner == "SS" and "cell (SIMPLE" in Path(lib).read_text()
    assert "max_transition : 4000;" in Path(lib).read_text()
    assert "max_transition : 100;" in Path(lib).read_text()
    again = tmp_path / "again"
    assert (
        _run(source, again, "--vt", "RVT", "LVT", "--corner", "TT", "SS").returncode
        == 0
    )
    assert (root / "klt-pdk-sources.json").read_bytes() == (
        again / "klt-pdk-sources.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "pattern",
    [
        "*/LEF/*_R_*",
        "*/GDS/*_R_*",
        "*/techlef_misc/*.lef",
        "*/LIB/NLDM/*_SEQ_RVT_TT_*",
        "*/cdslib/*/*.layermap",
    ],
)
def test_missing_source_fails_without_partial_root(source, tmp_path, pattern):
    missing = next(source.glob(pattern))
    missing.unlink()
    output = tmp_path / "root"
    result = _run(source, output)
    assert result.returncode == 1
    assert missing.name in result.stderr
    assert not output.exists()


def test_changed_input_is_recorded_and_output_is_publish_once(source, tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run(source, first).returncode == 0
    manifest = "klt-pdk-sources.json"
    recorded = json.loads((first / manifest).read_text())
    path = source / PDK_DIR / LAYER_MAP
    path.write_text("M3 NET 30 0\n")
    repeat = _run(source, first)
    assert repeat.returncode == 1 and "already exists" in repeat.stderr
    assert _run(source, second).returncode == 0
    changed = json.loads((second / manifest).read_text())
    assert changed["commits"] == recorded["commits"]
    assert (
        changed["inputs"][path.relative_to(source).as_posix()]
        != recorded["inputs"][path.relative_to(source).as_posix()]
    )
    assert (
        changed["outputs"]["klayout/tech/asap7.map"]
        != recorded["outputs"]["klayout/tech/asap7.map"]
    )


def test_inconsistent_corner_is_rejected(source, tmp_path):
    path = next(source.glob("*/LIB/NLDM/*_SIMPLE_RVT_TT_*"))
    path.write_text(
        path.read_text().replace("  cell (", "  nom_voltage : 0.77;\n  cell (")
    )
    result = _run(source, tmp_path / "root")
    assert result.returncode == 1 and "nom_voltage" in result.stderr
    assert not (tmp_path / "root").exists()


def test_seven_zip_liberty_is_unpacked(source, tmp_path):
    binary = shutil.which("7zz") or shutil.which("7z")
    if not binary:
        pytest.skip("requires 7-Zip")
    plain = next(source.glob("*/LIB/NLDM/*_SEQ_RVT_TT_*"))
    archive = plain.with_suffix(".lib.7z")
    subprocess.run(
        [binary, "a", str(archive), plain.name],
        cwd=plain.parent,
        check=True,
        capture_output=True,
    )
    plain.unlink()
    root = tmp_path / "root"
    result = _run(source, root)
    assert result.returncode == 0, result.stderr
    inputs = json.loads((root / "klt-pdk-sources.json").read_text())["inputs"]
    assert (
        inputs[archive.relative_to(source).as_posix()]
        == hashlib.sha256(archive.read_bytes()).hexdigest()
    )


def test_corrected_ff_keeps_its_voltage_and_condition_values(source, tmp_path):
    folder = source / CELL_DIR / "LIB/NLDM"
    for tt in folder.glob("*_RVT_TT_*.lib"):
        ff = folder / tt.name.replace("_TT_", "_FF_")
        groups = """  input_voltage (Vinput) { vil : 0; }
  output_voltage (Voutput) { vol : 0; }
"""
        condition = "PVT_0P77V_0C"
        if "_SIMPLE_" in tt.name:
            condition = "FF_0.77_0"
            groups = ""
            ff = folder / ff.name.replace("211120", "250407")
        attrs = (
            f"  operating_conditions ({condition}) {{ voltage : 0.77; }}\n"
            f"  default_operating_conditions : {condition};\n"
        )
        table = (
            "input_net_transition"
            if "_SIMPLE_" in tt.name
            else "total_output_net_capacitance"
        )
        attrs += (
            f"  lu_table_template (delay_template_7x7) {{ variable_1 : {table}; }}\n"
        )
        ff.write_text(tt.read_text().replace("  cell (", attrs + groups + "  cell ("))
    output = tmp_path / "good"
    result = _run(source, output, "--corner", "FF")
    assert result.returncode == 0, result.stderr
    merged = next(output.glob("libs/*/lib/*.lib")).read_text()
    assert "lu_table_template (simple_ff_250407_delay_template_7x7)" in merged
    assert "lu_table_template (delay_template_7x7)" in merged
    simple = next(folder.glob("*250407.lib"))
    simple.write_text(simple.read_text().replace("voltage : 0.77;", "voltage : 0.70;"))
    result = _run(source, tmp_path / "invalid", "--corner", "FF")
    assert result.returncode == 1 and "operating_conditions" in result.stderr


@pytest.mark.skipif(
    not os.environ.get("KLT_ASAP7_SOURCE")
    or not shutil.which("openroad")
    or not shutil.which("yosys"),
    reason="set KLT_ASAP7_SOURCE and install Yosys and OpenROAD",
)
def test_real_public_asap7_flow(tmp_path, monkeypatch):
    import klayout.db as db

    root = tmp_path / "assembled"
    result = _run(Path(os.environ["KLT_ASAP7_SOURCE"]), root)
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv("PDK_ROOT", str(root))
    # The containerized OpenROAD wrapper mounts its working directory.
    monkeypatch.chdir(tmp_path)

    def klt(verb, request=None):
        cmd = [sys.executable, "-m", "klayout_tools.cli", *verb.split()]
        if request is not None:
            path = tmp_path / (verb + ".json")
            path.write_text(json.dumps(request))
            cmd.append(str(path))
        run = subprocess.run(
            [*cmd, "--pdk", "asap7", "--pdk-root", str(root), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=360,
        )
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)

    assert klt("pdk find")["layout"] == "descriptor"
    _put(
        tmp_path / "counter.v",
        """module counter(input clk, rst_n, en, output reg [7:0] count);
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) count <= 0;
  else if (en) count <= count + 1;
end
endmodule
""",
    )
    pdk_spec = {"cell_library": "asap7sc7p5t_rvt", "corner": "TT"}
    synth = klt(
        "synthesize",
        {
            "hdl_toplevel": "counter",
            "pdk": pdk_spec,
            "sources": ["counter.v"],
            "run_id": "counter",
        },
    )
    assert synth["status"] == "ok" and synth["instance_count"] > 0
    netlist = tmp_path / ".klt/synthesize/counter/counter_synth.v"
    constraints = {"clock_port": "clk", "clock_period_ns": 1.0}
    fixture = SCRIPT.parents[1] / "tests/fixtures/place_and_route_drcfill/request.json"
    request = json.loads(fixture.read_text())
    request.update(
        hdl_toplevel="counter",
        netlist=str(netlist),
        pdk={**pdk_spec, "sweep_corners": []},
        constraints=constraints,
    )
    routed = klt("place-and-route", request)
    assert routed["status"] == "ok" and routed["layer_map"]["resolution"] == "exact"
    assert all("layer map" not in warning.lower() for warning in routed["warnings"])
    layout = db.Layout()
    layout.read(routed["gds_path"])
    top = layout.top_cell()
    for layer in (20, 30):  # Upstream M2 NET and M3 NET
        assert top.shapes(layout.layer(layer, 0)).size() > 0
    timing = klt(
        "sta",
        {
            "def": routed["def_path"],
            "hdl_toplevel": "counter",
            "pdk": pdk_spec,
            "constraints": constraints,
        },
    )
    assert timing["status"] == "ok" and isinstance(
        timing["worst_slack_ns"], (int, float)
    )
