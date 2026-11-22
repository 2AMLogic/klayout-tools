"""A schematic DUT and extracted top can have different SPICE names."""

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE as real_ngspice
from klayout_tools import pex, pex_binding
from klayout_tools.pex_dry_run import run_pex_dry_run
from test_pex import (
    _fake_async_sim,
    _make_sky130_poly_resistor_layout,
    _write_request,
    _write_testbench,
)


@pytest.fixture
def design(tmp_path):
    gds = tmp_path / "layout.gds"
    _make_sky130_poly_resistor_layout("LAYOUT_RES").write(str(gds))
    dut = tmp_path / "res.spice"
    dut.write_text(
        ".subckt RES RA RB\nX1 RA RB leaf\n.ends RES\n"
        ".subckt leaf A B\nR1 A B 289.2\n.ends leaf\n"
    )
    tb = _write_testbench(tmp_path / "tb.spice", dut)
    tb.write_text(tb.read_text().replace("Xres RA RB RES", "Xres RA RB rEs"))
    request = _write_request(tmp_path / "request.json", tb)
    return gds, dut, tb, request


def _kwargs(tmp_path):
    return dict(
        top="LAYOUT_RES",
        schematic_top="res",
        backend="local",
        declared_pins=frozenset({"RA", "RB"}),
        rename_declared_pins=True,
        artifacts_dir=str(tmp_path / "artifacts"),
    )


@real_ngspice
@pytest.mark.parametrize("flatten", [False, True])
def test_real_resistor_measures_both_sides(tmp_path, design, flatten):
    gds, dut, tb, request = design
    original = {path: path.read_bytes() for path in design}
    report = pex.run_pex(
        str(gds),
        [str(request)],
        "sky130",
        flatten_schematic=flatten,
        **_kwargs(tmp_path),
    )
    (row,) = report["delta"]
    assert row["measurement"] == "vout"
    assert row["schematic_value"] == pytest.approx(1.39621, rel=1e-4)
    assert row["extracted_value"] == pytest.approx(1.11626, rel=1e-4)
    assert report["status"] == "pass"
    for path, contents in original.items():
        assert path.read_bytes() == contents
    if flatten:
        assert report["schematic_flatten"]["subckts"] == ["RES"]
        assert report["schematic_flatten"]["inlined_subckts"] == ["leaf"]


def test_only_instance_target_tokens_change():
    body = (
        "* Xcomment a b rEs\n.control\nxdisplay a b rEs\n.endc\n"
        "Xother a b mystery ; rEs\n"
        "Xob RES a ; RES\n* interleaved\n + rEs PARAMS: width = {2 * RES}\n"
        "xagain RA RB RES m = 2 $ RES\n"
    )
    expected = body.replace("+ rEs PARAMS:", "+ LAYOUT_RES PARAMS:").replace(
        "RB RES m", "RB LAYOUT_RES m"
    )
    assert pex_binding.retarget_calls(body, "RES", "LAYOUT_RES", 2) == expected


@pytest.mark.parametrize(
    "fault", ["missing_root", "unknown_target", "bad_arity", "second_testbench"]
)
def test_unresolved_binding_precedes_all_submissions(
    tmp_path, design, monkeypatch, fault
):
    gds, dut, tb, request = design
    requests = [str(request)]
    options = _kwargs(tmp_path)
    if fault == "second_testbench":
        tb = tmp_path / "tb2.spice"
        tb.write_text(design[2].read_text())
        requests.append(str(_write_request(tmp_path / "second.json", tb)))
    if fault == "missing_root":
        options["schematic_top"] = "no_root"
    elif fault == "bad_arity":
        tb.write_text(tb.read_text().replace("Xres RA RB rEs", "Xres RB rEs"))
    else:
        tb.write_text(tb.read_text().replace("Xres RA RB rEs", "Xres RA RB WRONG"))
    before = {path: path.read_bytes() for path in (*design, tb)}
    calls = []
    _fake_async_sim(monkeypatch, calls)
    options.update(backend="batch", submit_only=True)
    with pytest.raises(pex.PexError) as error:
        pex.run_pex(str(gds), requests, "sky130", **options)
    assert error.value.code == "dut_binding"
    assert options["schematic_top"] in str(error.value) and "LAYOUT_RES" in str(
        error.value
    )
    assert calls == []
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    ("pins", "code"),
    [("RB RA", "pin_order_mismatch"), ("RA RB RC", "pin_count_mismatch")],
)
def test_mapped_roots_preserve_pin_diagnostics(
    tmp_path, design, monkeypatch, pins, code
):
    gds, dut, tb, request = design
    dut.write_text(dut.read_text().replace(".subckt RES RA RB", f".subckt RES {pins}"))
    calls = []
    _fake_async_sim(monkeypatch, calls)
    options = _kwargs(tmp_path)
    options.update(backend="batch", submit_only=True)
    with pytest.raises(pex.PexError, match=code):
        pex.run_pex(str(gds), [str(request)], "sky130", **options)
    assert calls == []


def test_dry_run_validates_schematic_target_without_extraction(tmp_path, design):
    gds, dut, tb, request = design
    options = _kwargs(tmp_path)
    options.pop("artifacts_dir")
    tb.write_text(tb.read_text().replace("Xres RA RB rEs", "Xres RA RB UNKNOWN"))
    with pytest.raises(pex.PexError, match="no call to 'res'"):
        run_pex_dry_run(str(gds), [str(request)], "sky130", **options)


def test_async_manifest_binds_root_case_insensitively(tmp_path, design, monkeypatch):
    gds, dut, tb, request = design
    calls = []
    _fake_async_sim(monkeypatch, calls)
    options = _kwargs(tmp_path)
    options["backend"] = "batch"
    pex.run_pex(str(gds), [str(request)], "sky130", submit_only=True, **options)
    manifest = Path(options["artifacts_dir"]) / "pex-submit.json"
    assert json.loads(manifest.read_text())["schematic_top"] == "res"
    calls.clear()
    options["schematic_top"] = "RES"
    pex.run_pex(str(gds), [str(request)], "sky130", attach=str(manifest), **options)
    assert len(calls) == 2
    calls.clear()
    options["schematic_top"] = None
    with pytest.raises(pex.PexError, match="schematic DUT selection"):
        pex.run_pex(str(gds), [str(request)], "sky130", attach=str(manifest), **options)
    assert calls == []
