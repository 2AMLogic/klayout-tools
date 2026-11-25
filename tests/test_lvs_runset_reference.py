"""Issue #4805: ``klayout-runset`` accepts ``reference.form: "subckt-call"``
and converts the reference before the runset sees it. Split from
``test_lvs_klayout_runset`` (file-size ratchet); reuses its fake-klayout rig."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lvs_tmp_repo
import test_lvs_klayout_runset as base
from klayout_tools import lvs_runset
from klayout_tools.lvs import LvsError, rerun_lvs_report, run_lvs

fake_klayout = base.fake_klayout  # pytest collects the fixture by module-level name
_request = base._request
_unresolved = base._unresolved
tmp_path = lvs_tmp_repo.tmp_path  # pytest collects the fixture by module-level name


MIM_SPICE = (
    ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n"
    "XC1 a b sky130_fd_pr__cap_mim_m3_1 W=10u L=5u\n.ends\n"
)


def _mim_request(tmp_path, **reference):
    request = json.loads(_request(tmp_path, 1))
    (tmp_path / "ref.sp").write_text(MIM_SPICE)
    request["reference"].update(reference)
    return request


def test_subckt_call_converts_mim_for_the_runset(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    seen = {}
    real = lvs_runset._run_runset

    def spy(**kw):
        seen["text"] = open(kw["reference"]).read()
        return real(**kw)

    lvs_runset._run_runset = spy
    try:
        report = run_lvs(json.dumps(request))
    finally:
        lvs_runset._run_runset = real
    # W=10u L=5u -> A=50 um^2 = 50P, P=2*(10+5)=30 um = 30U (numeric, not assumed).
    assert "C1 a b sky130_fd_pr__model__cap_mim A=50P P=30U" in seen["text"]
    assert "XC1" not in seen["text"]
    # Issue #4817: the real sky130.lvs reader takes a positional ``0`` as a net.
    assert " 0 " not in seen["text"].split("C1", 1)[1].split("\n", 1)[0]
    assert not _unresolved(report)
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": "sky130",
        "device_map": None,
    }
    # The caller's file is untouched and still the echoed reference.
    assert "XC1" in (tmp_path / "ref.sp").read_text()
    assert report["reference"] == str(tmp_path / "ref.sp")


def test_without_form_mim_is_still_unresolved(tmp_path, fake_klayout):
    report = run_lvs(json.dumps(_mim_request(tmp_path)))
    assert len(_unresolved(report)) == 1
    assert "reference_conversion" not in report


def test_other_reference_keys_still_refused(tmp_path, fake_klayout):
    for extra in (
        {"deck": "sky130"},
        {"device_bulk": {"x": "y"}},
        {"form": "subckt-call", "device_bulk": {"x": "y"}},
    ):
        with pytest.raises(LvsError, match="not supported by engine"):
            run_lvs(json.dumps(_mim_request(tmp_path, **extra)))
    with pytest.raises(LvsError, match="gate-level-verilog"):
        run_lvs(json.dumps(_mim_request(tmp_path, form="gate-level-verilog")))


def test_subckt_call_unknown_device_is_an_error(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    (tmp_path / "ref.sp").write_text(
        MIM_SPICE.replace("sky130_fd_pr__cap_mim_m3_1", "no_such_dev")
    )
    with pytest.raises(LvsError, match="could not convert subckt-call"):
        run_lvs(json.dumps(request))


def test_subckt_call_report_replays_under_check_rerun(tmp_path, fake_klayout):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    report = run_lvs(json.dumps(request))
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    result = rerun_lvs_report(str(committed))
    assert result["status"] == "match", result
    assert not result.get("drift"), result


# --- Issue #5402: deck DEVICE_LIST pass-through and top renaming ------------

RF_SPICE = (
    ".subckt TOP a b\nXL1 a b inductor W=10u L=5u\n"
    "XC1 a b sky130_fd_pr__cap_mim_m3_1 W=10u L=5u\n.ends TOP\n"
)


def _capture_reference(request):
    seen = {}
    real = lvs_runset._run_runset

    def spy(**kw):
        seen["text"] = open(kw["reference"]).read()
        seen["path"] = kw["reference"]
        return real(**kw)

    lvs_runset._run_runset = spy
    try:
        report = run_lvs(json.dumps(request))
    finally:
        lvs_runset._run_runset = real
    return report, seen


def _rf_request(tmp_path, device_list=("inductor",), **reference):
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130", **reference)
    (tmp_path / "ref.sp").write_text(RF_SPICE)
    names = ", ".join(f'"{n}".upcase' for n in device_list)
    (tmp_path / "pdk.lvs").write_text(f"DEVICE_LIST = [{names}]\n")
    return request


def test_deck_device_list_subckt_passes_through(tmp_path, fake_klayout):
    _, seen = _capture_reference(_rf_request(tmp_path))
    assert "XL1 a b inductor W=10u L=5u" in seen["text"]  # untouched
    assert "C1 a b sky130_fd_pr__model__cap_mim" in seen["text"]  # curated wins


def test_curated_name_in_device_list_still_converts(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, device_list=("sky130_fd_pr__cap_mim_m3_1",))
    with pytest.raises(LvsError, match="inductor"):
        run_lvs(json.dumps(request))  # inductor not listed here -> still loud
    request = _rf_request(
        tmp_path, device_list=("inductor", "sky130_fd_pr__cap_mim_m3_1")
    )
    _, seen = _capture_reference(request)
    assert "XC1" not in seen["text"]


def test_unlisted_unknown_subckt_still_refused(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, device_list=("other_dev",))
    with pytest.raises(LvsError, match="could not convert subckt-call"):
        run_lvs(json.dumps(request))


def test_differing_tops_rename_reference_copy_only(tmp_path, fake_klayout):
    request = _rf_request(tmp_path, top="TOP")
    request["layout"]["top"] = "LAYTOP"
    (tmp_path / "ref.sp").write_text(RF_SPICE + ".subckt TOPPER x\n.ends TOPPER\n")
    report, seen = _capture_reference(request)
    assert ".subckt LAYTOP a b" in seen["text"]
    assert ".ends LAYTOP" in seen["text"]
    assert ".subckt TOPPER x" in seen["text"]  # not a substring match
    assert ".subckt TOP a b" in (tmp_path / "ref.sp").read_text()
    assert report["reference_top"] == "TOP"


def test_matching_or_absent_tops_leave_reference_unrenamed(tmp_path, fake_klayout):
    request = json.loads(_request(tmp_path, 1))
    request["layout"]["top"] = "TOP"
    request["reference"]["top"] = "TOP"
    _, seen = _capture_reference(request)
    assert seen["path"] == str(tmp_path / "ref.sp")  # no work copy
    request["reference"].pop("top")
    _, seen = _capture_reference(request)
    assert seen["path"] == str(tmp_path / "ref.sp")


def _sg13g2_text(tmp_path, text, **reference_options):
    from klayout_tools.lvs_runset_reference import convert_reference

    source = tmp_path / "ref.sp"
    source.write_text(text)
    deck = tmp_path / "sg13g2.lvs"
    deck.write_text("# IHP native runset\n")
    request = {
        "reference": {"form": "subckt-call", "top": "SCHEM", **reference_options},
        "layout": {"top": "LAYOUT"},
    }
    return Path(
        convert_reference(str(source), request, str(tmp_path), str(deck))
    ).read_text()


@pytest.mark.parametrize(
    ("call", "element"),
    [
        (
            "Xcoil la lb sub inductor W=5.3u S=8.9u NR_R=1",
            "Lcoil la lb sub inductor W=5.3u S=8.9u NR_R=1",
        ),
        (
            "XC1 g1 w g2 sub SG13_HV_SVARICAP\n+ PARAMS: Nx=2 W=3.74u",
            "CC1 g1 w g2 sub SG13_HV_SVARICAP Nx=2 W=3.74u",
        ),
    ],
)
def test_sg13g2_native_reader_receives_passive_element(tmp_path, call, element):
    converted = _sg13g2_text(tmp_path, f".subckt SCHEM\n{call}\n.ends SCHEM\n")
    assert element in converted.splitlines()
    assert ".subckt LAYOUT" in converted


@pytest.mark.parametrize(
    "call", ["Xcoil a b inductor W=1u", "Xvar a b c sg13_hv_svaricap W=2u"]
)
def test_sg13g2_native_passive_requires_all_terminals(tmp_path, call):
    with pytest.raises(LvsError, match="terminals"):
        _sg13g2_text(tmp_path, f".subckt SCHEM\n{call}\n.ends SCHEM\n")


def test_sg13g2_explicit_mapping_and_defined_rf_subcircuit_take_precedence(tmp_path):
    call = "Xcoil a b sub inductor W=5u"
    text = f".subckt SCHEM\n{call}\n.ends SCHEM\n"
    converted = _sg13g2_text(
        tmp_path,
        text,
        device_map={"inductor": {"kind": "inductor", "class": "my_coil"}},
    )
    assert "Xcoil a b sub my_coil" in converted
    (tmp_path / "cells.sp").write_text(
        ".subckt inductor a b sub\nR1 a b 10\n.ends inductor\n"
    )
    assert call in _sg13g2_text(tmp_path, ".include cells.sp\n" + text)


# --- Issue #7836: SG13G2 bipolar and MOS calls reach the native reader as cards ---


def _schem(call: str) -> str:
    return f".subckt SCHEM\n{call}\n.ends SCHEM\n"


@pytest.mark.parametrize(
    ("call", "card"),
    [
        (
            "XQ2 net4 net5 net6 sub npn13G2v Nx=1 El=1.15",
            "QXQ2 net4 net5 net6 sub npn13G2v le=1.15u we=120n m=1",
        ),
        ("Xq3 c b e s npn13G2v nx=2 EL=1", "QXq3 c b e s npn13G2v le=1u we=120n m=2"),
        (
            "XQ4 c b e s npn13G2l Nx=3 El=2.5",
            "QXQ4 c b e s npn13G2l le=2.5u we=70n m=3",
        ),
        ("XQ5 c b e s npn13G2 Nx=4", "QXQ5 c b e s npn13G2 le=900n we=70n m=4"),
        (
            "XQ6 c b e s npn13G2v\n+ PARAMS: El=1.0",
            "QXQ6 c b e s npn13G2v le=1.0u we=120n m=1",
        ),
    ],
)
def test_sg13g2_bipolar_call_becomes_the_native_q_card(tmp_path, call, card):
    # The pinned xschem symbols' ``lvs_format``: C/B/E/S pins, ``m=Nx`` and
    # ``le=El*1e-6`` (written as the ``u`` suffix), a fixed per-model ``we``.
    converted = _sg13g2_text(tmp_path, _schem(call))
    assert card in converted.splitlines()
    assert not [x for x in converted.splitlines() if x[:1] in "Xx" and "npn" in x]


@pytest.mark.parametrize(
    "model", ["sg13_hv_nmos", "sg13_hv_pmos", "sg13_lv_nmos", "sg13_lv_pmos"]
)
def test_sg13g2_mos_call_keeps_its_runset_class(tmp_path, model):
    call = f"XM1 d g s b {model} W=1u L=0.45u ng=2 m=3"
    converted = _sg13g2_text(tmp_path, _schem(call))
    assert f"MXM1 d g s b {model} W=1u L=0.45u ng=2 m=3" in converted.splitlines()
    assert "nfet" not in converted and "pfet" not in converted


@pytest.mark.parametrize(
    "call",
    [
        "XQ1 c b e s npn13G2v Nx=1",  # no El: the symbol's LVS form needs it
        "XQ1 c b e s npn13G2v Nx=1 El={w}",  # not a plain number
        "XQ1 c b e s npn13G2v Nx=1 El=1u",  # a unit suffix on a micrometre value
        "XQ1 c b e npn13G2v Nx=1 El=1",  # three terminals
        "XM1 d g s sg13_hv_nmos W=1u L=1u",  # three terminals
        "XM1 d g s b sg13_hv_nmos W=1u",  # no L
    ],
)
def test_sg13g2_unconvertible_call_stays_a_call(tmp_path, call):
    # Left exactly as written, for the unresolved-call scan to name.
    converted = _sg13g2_text(tmp_path, _schem(call))
    assert call in converted.splitlines()


def test_sg13g2_explicit_device_map_and_child_circuits_take_precedence(tmp_path):
    text = _schem(
        "XQ1 c b e s npn13G2v Nx=1 El=1\n"
        "XM1 d g s b sg13_hv_nmos W=1u L=1u\n"
        "Xchild a b c s child"
    )
    mapped = _sg13g2_text(
        tmp_path,
        text
        + ".subckt child a b c s\n.ends child\n"
        + ".subckt npn13G2v a b c d\n.ends npn13G2v\n"
        + ".subckt sg13_hv_nmos a b c d\n.ends sg13_hv_nmos\n",
        device_map={
            "npn13G2v": {"kind": "bipolar", "class": "my_npn"},
            "sg13_hv_nmos": "my_nmos",
        },
    ).splitlines()
    elements = [line for line in mapped if not line.startswith(".")]
    assert not any("npn13G2v" in line or "sg13_hv_nmos" in line for line in elements)
    assert any(" my_npn " in x or x.endswith(" my_npn") for x in mapped)
    assert any(" my_nmos " in x for x in mapped)
    assert "Xchild a b c s child" in mapped  # genuine hierarchy untouched


@pytest.mark.parametrize("included", [False, True], ids=["inline", "included"])
@pytest.mark.parametrize(
    ("model", "params"),
    [("npn13G2", ""), ("sg13_hv_nmos", " W=1u L=0.5u")],
)
def test_sg13g2_defined_native_name_keeps_reachable_child(
    tmp_path, model, params, included
):
    import klayout.db as kdb

    call = f"Xchild a b c d {model}{params}"
    definition = f".subckt {model} a b c d{params}\nR1 a b 1k\nR2 c d 2k\n.ends\n"
    if included:
        (tmp_path / "cells.sp").write_text(definition)
        definition = ".include cells.sp\n"
    converted = _sg13g2_text(tmp_path, _schem(call) + definition)
    assert call in converted.splitlines()

    netlist = kdb.Netlist()
    netlist.read(str(tmp_path / "reference_converted.sp"), kdb.NetlistSpiceReader())
    top = netlist.circuit_by_name("LAYOUT")
    (child,) = top.each_subcircuit()
    child_circuit = child.circuit_ref()
    assert child_circuit.name.partition("(")[0] == model.upper()
    assert [device.device_class().name for device in child_circuit.each_device()] == [
        "RES",
        "RES",
    ]


def test_sg13g2_native_primitives_ignore_other_decks(tmp_path, fake_klayout):
    # A non-SG13G2 runset's reader keeps its own conversion.
    request = _mim_request(tmp_path, form="subckt-call", deck="sky130")
    (tmp_path / "ref.sp").write_text(_schem("XQ1 c b e s npn13G2v Nx=1 El=1"))
    _, seen = _capture_reference(request)
    assert "XQ1 c b e s npn13G2v Nx=1 El=1" in seen["text"].splitlines()


def _sg13g2_request(tmp_path, spice, **reference):
    request = _mim_request(tmp_path, form="subckt-call", **reference)
    (tmp_path / "ref.sp").write_text(spice)
    request["deck_file"] = str(tmp_path / "sg13g2.lvs")
    (tmp_path / "sg13g2.lvs").write_text("# IHP native runset\n")
    return request


def test_sg13g2_declared_class_does_not_exempt_a_lost_call(tmp_path, fake_klayout):
    # The native reader converts no ``X`` card, so a callee that merely names a
    # declared class (here the fixture layout's ``NMOS``) is still lost.
    request = _sg13g2_request(
        tmp_path,
        ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n"
        "XQ1 c b a d NMOS Nx=1\nXQ2 c b a d npn13G2v Nx=1\n.ends\n",
    )
    found = _unresolved(run_lvs(json.dumps(request)))
    assert sorted(
        (m["instance"]["reference"], m["subcircuit"]["reference"]) for m in found
    ) == [("Q1", "NMOS"), ("Q2", "NPN13G2V")]
    assert all(m["severity"] == "error" for m in found)


def test_sg13g2_lost_call_stays_visible_beside_a_converted_one(tmp_path, fake_klayout):
    request = _sg13g2_request(
        tmp_path,
        ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n"
        "Xgood c b a d npn13G2v Nx=1 El=1.15\n"
        "Xlost c b a d npn13G2v Nx=1\nXchild c b a d CHILD\n.ends\n"
        ".subckt CHILD p q r s\n.ends CHILD\n",
    )
    report, seen = _capture_reference(request)
    assert "QXgood c b a d npn13G2v le=1.15u we=120n m=1" in seen["text"]
    found = _unresolved(report)
    assert [m["instance"]["reference"].upper() for m in found] == ["LOST"]
    assert found[0]["circuit"]["reference"].upper() == "TOP"
    assert found[0]["subcircuit"]["reference"] == "NPN13G2V"
    assert report["status"] == "mismatch"
    assert report["category_error_counts"]["reference.unresolved_subckt_call"] == 1


def test_sg13g2_report_replays_with_its_conversion_inputs(tmp_path, fake_klayout):
    request = _sg13g2_request(
        tmp_path,
        ".subckt TOP\nM1 c b a d NMOS L=0.2u W=0.4u\n"
        "Xgood c b a d npn13G2v Nx=1 El=1.15\nXlost c b a d npn13G2v Nx=1\n.ends\n",
        device_map={"unused_dev": "nfet"},
    )
    report = run_lvs(json.dumps(request))
    assert report["reference_conversion"] == {
        "form": "subckt-call",
        "deck": None,
        "device_map": {"unused_dev": "nfet"},
    }
    assert report["status"] == "mismatch"
    assert [m["instance"]["reference"].upper() for m in _unresolved(report)] == ["LOST"]
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    # ``status`` here is the replay's own verdict: the rerun rebuilt the same
    # conversion from the echoed inputs, so it reproduced the committed
    # report -- converted call, named lost call and all -- with no drift.
    result = rerun_lvs_report(str(committed))
    assert result["status"] == "match", result
    assert not result.get("drift"), result


# --- Issue #7918: commented xschem wrapper and headerless implicit .TOP ------


def _read_converted(tmp_path, ref_dir, text, *, layout_top="lay_block", **ref_kw):
    import klayout.db as kdb

    from klayout_tools.lvs_runset_reference import convert_reference

    source = ref_dir / "top.sp"
    source.write_bytes(text.encode("utf-8"))
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    req = {"layout": {"top": layout_top}, "reference": dict(ref_kw)}
    out = convert_reference(str(source), req, str(work), str(deck))
    nl = kdb.Netlist()
    nl.read(out, kdb.NetlistSpiceReader())
    return out, source, nl


@pytest.mark.parametrize("ref_top", [None, "my_block", ".TOP"])
def test_xschem_wrapper_supplies_top_pins_and_relative_include(tmp_path, ref_top):
    sub = tmp_path / "schem"
    sub.mkdir()
    leaf = sub / "leaf.sp"
    leaf_bytes = b".subckt my_leaf a y vdd vss\nR1 a y 1k\n.ends\n"
    leaf.write_bytes(leaf_bytes)
    raw = (
        "* header\n.include leaf.sp\n**.subckt my_block a y\n*+ vdd vss\n"
        "X0 a y vdd vss my_leaf\n*+ note\n**.ends\n.end\n"
    )
    kw = {} if ref_top is None else {"top": ref_top}
    out, source, nl = _read_converted(tmp_path, sub, raw, **kw)
    assert out != str(source)
    assert source.read_bytes() == raw.encode("utf-8")
    assert leaf.read_bytes() == leaf_bytes
    top = nl.circuit_by_name("LAY_BLOCK")
    assert top is not None
    assert [p.name() for p in top.each_pin()] == ["A", "Y", "VDD", "VSS"]
    assert [s.circuit_ref().name for s in top.each_subcircuit()] == ["MY_LEAF"]
    assert nl.circuit_by_name("MY_LEAF") is not None


def test_implicit_top_isolates_instances_with_zero_pins_and_subckt_call(tmp_path):
    raw = (
        ".option scale=1u\nXM1 d g s b sky130_fd_pr__nfet_01v8 W=1u L=0.15u\n"
        "X0 a y my_leaf\n.subckt my_leaf a y\nR1 a y 1k\n.ends\n.end\n"
    )
    out, source, nl = _read_converted(
        tmp_path, tmp_path, raw, top=".TOP", form="subckt-call", deck="sky130"
    )
    assert source.read_text() == raw
    assert "M1 d g s b nfet L=0.15U W=1U" in Path(out).read_text()
    top = nl.circuit_by_name("LAY_BLOCK")
    assert top is not None and list(top.each_pin()) == []
    assert [s.circuit_ref().name for s in top.each_subcircuit()] == ["MY_LEAF"]
    leaf = nl.circuit_by_name("MY_LEAF")
    assert [d.device_class().name for d in leaf.each_device()] == ["RES"]
