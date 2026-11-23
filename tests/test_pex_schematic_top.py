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
@pytest.mark.parametrize(
    "pin_list", ["RA RB", "(RA RB)", "(RA,RB)", "RA,RB", "RA , RB"]
)
def test_real_resistor_measures_both_sides(tmp_path, design, flatten, pin_list):
    gds, dut, tb, request = design
    tb.write_text(tb.read_text().replace("Xres RA RB rEs", f"Xres {pin_list} rEs"))
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


def test_parenthesized_pin_list_validation_preserves_target_offset():
    body = "Xcall (RES RES) RES params: m=2 ; RES\n"
    assert pex_binding.retarget_calls(body, "RES", "LAYOUT", 2) == body.replace(
        ") RES", ") LAYOUT"
    )
    with pytest.raises(ValueError, match="3 nodes for 2 pins"):
        pex_binding.retarget_calls("Xcall (a b c) RES", "RES", "LAYOUT", 2)


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


_PIN_LISTS = ["RA RB", "RA,RB", "RA , RB", "RA ,RB", "RA, RB", "(RA,RB)", "(RA , RB)"]


@pytest.mark.parametrize("pins", _PIN_LISTS)
def test_every_comma_spelling_counts_two_pins(pins):
    body = f"Xcall {pins} RES\n"
    assert pex_binding.retarget_calls(body, "RES", "LAYOUT", 2) == body.replace(
        "RES", "LAYOUT"
    )


@pytest.mark.parametrize("pins", ["RA", "RA,RB,RC", "RA , RB , RC", "(RA,RB,RC)"])
def test_comma_spellings_still_reject_a_wrong_pin_count(pins):
    with pytest.raises(ValueError, match="nodes for 2 pins"):
        pex_binding.retarget_calls(f"Xcall {pins} RES\n", "RES", "LAYOUT", 2)


_PRIVATE = ".subckt load p n\n.subckt res a b\nRlocal a b 9000\n.ends res\n"


def test_private_subckt_of_the_dut_name_is_not_retargeted():
    body = (
        f"{_PRIVATE}Xprivate p n RES\n.ends load\n"
        ".subckt wrap p n\nXin p n RES\n.ends wrap\n"
        "Xunit RA RB RES\nXload RB 0 load\n"
    )
    expected = body.replace("Xin p n RES", "Xin p n LAYOUT").replace(
        "Xunit RA RB RES", "Xunit RA RB LAYOUT"
    )
    assert pex_binding.retarget_calls(body, "RES", "LAYOUT", 2) == expected


def test_a_call_resolving_only_to_a_private_subckt_is_not_a_dut_call(tmp_path):
    body = f"{_PRIVATE}Xprivate p n RES\n.ends load\nRother RA RB 1\n"
    assert pex_binding.retarget_calls(body, "RES", "LAYOUT", 2) is None
    schematic = tmp_path / "dut.spice"
    schematic.write_text(".subckt RES RA RB\nR1 RA RB 1\n.ends RES\n")
    testbench = tmp_path / "tb.spice"
    testbench.write_text(body)
    with pytest.raises(ValueError, match="no call to 'RES'"):
        pex_binding.bind("RES", "LAYOUT", str(schematic), [str(testbench)], None)


def test_a_private_subckt_named_like_the_extracted_top_is_refused():
    enclosed = (
        ".subckt wrap p n\n.subckt layout a b\n.ends layout\nXin p n RES\n.ends wrap\n"
    )
    with pytest.raises(ValueError, match="private .subckt 'LAYOUT'"):
        pex_binding.retarget_calls(enclosed, "RES", "LAYOUT", 2)
    elsewhere = (
        ".subckt other a b\n.subckt layout x y\n.ends layout\n.ends other\n"
        "Xin p n RES\n"
    )
    assert pex_binding.retarget_calls(
        elsewhere, "RES", "LAYOUT", 2
    ) == elsewhere.replace("n RES", "n LAYOUT")


@real_ngspice
@pytest.mark.parametrize("flatten", [False, True])
def test_real_private_subckt_of_the_dut_name_keeps_its_own_definition(
    tmp_path, design, flatten
):
    gds, dut, tb, request = design
    measured = {}
    for private in ("AUX_R", "rEs"):
        tb.write_text(
            f'.include "{dut}"\n.model res_generic_po r\n'
            ".subckt load p n\n"
            f".subckt {private} a b\nRlocal a b 9000\n.ends {private}\n"
            f"Xprivate p n {private}\n.ends load\n"
            "Vdd RA 0 DC 1.8\nXres RA RB rEs\nXload RB 0 load\n"
        )
        report = pex.run_pex(
            str(gds),
            [str(request)],
            "sky130",
            flatten_schematic=flatten,
            **_kwargs(tmp_path),
        )
        assert report["status"] == "pass"
        (row,) = report["delta"]
        measured[private] = (row["schematic_value"], row["extracted_value"])
    assert measured["rEs"] == pytest.approx(measured["AUX_R"], rel=1e-9)


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


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    [
        ("file_scope_collision", "instead of the extracted top"),
        ("private_root_only", "no .subckt 'res'"),
        ("parenthesized_order", "pin_order_mismatch"),
        ("comma_arity", "1 nodes for 2 pins"),
    ],
)
def test_mapped_root_refusals_before_either_leg(
    tmp_path, design, monkeypatch, fault, diagnostic
):
    gds, dut, tb, request = design
    if fault == "file_scope_collision":
        with tb.open("a") as handle:
            handle.write("\n.subckt layout_res p n\nRfake p n 1\n.ends\n")
    elif fault == "private_root_only":
        dut.write_text(".subckt fixture p n\n" + dut.read_text() + ".ends fixture\n")
    elif fault == "parenthesized_order":
        dut.write_text(dut.read_text().replace("RES RA RB", "RES (RB RA)"))
    else:
        dut.write_text(dut.read_text().replace("RES RA RB", "RES RA,RB"))
        tb.write_text(tb.read_text().replace("Xres RA RB", "Xres RA"))
    calls = []
    _fake_async_sim(monkeypatch, calls)
    options = _kwargs(tmp_path)
    options.update(backend="batch", submit_only=True)
    with pytest.raises(pex.PexError, match=diagnostic):
        pex.run_pex(str(gds), [str(request)], "sky130", **options)
    assert calls == []


@real_ngspice
@pytest.mark.parametrize("header", ["(RA RB)", "RA,RB", "(RA, RB)"])
def test_real_public_interface_after_private_namesake(tmp_path, design, header):
    gds, dut, tb, request = design
    dut.write_text(
        ".subckt fixture a b c\n.subckt RES a b c\nRprivate a b 1\n"
        ".ends RES\n.ends fixture\n"
        f".subckt RES {header}\nRpublic RA RB 289.2\n.ends RES\n"
    )
    report = pex.run_pex(str(gds), [str(request)], "sky130", **_kwargs(tmp_path))
    assert report["status"] == "pass"
    (row,) = report["delta"]
    assert row["schematic_value"] == pytest.approx(1.39621, rel=1e-4)
    assert row["extracted_value"] == pytest.approx(1.11626, rel=1e-4)
