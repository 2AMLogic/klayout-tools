"""`--parasitic-variant drop-r-body` (issue #7317): class-aware body/bulk
terminal handling for the star parasitics topology.

A near-floating body terminal on its own star leaf makes ngspice transient
Newton iteration fail for OSDI compact models (the reporter's
`timestep_too_small`); keeping body-class terminals on the net hub -- and
re-splitting the net's total R across the signal terminals only -- is the
opt-in fix. The device-class table is exact-type-keyed because KLayout
subclasses its device classes (MOS4 subclasses MOS3, BJT4 subclasses BJT3,
the WithBulk classes subclass their plain ones), so a BJT3's `B` (the
*base*, a signal terminal) must never match.

The CLI/report integration tests ride the same sky130 inverter fixture and
`_write_gds` helper the `drop-r` tests in `tests/test_extract.py` use; the
device-class coverage MOS4/BJT3/BJT4/WithBulk needs no GDS deck at all, so
those cases drive `_inject_parasitics` over an in-memory circuit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.extract_parasitic_body import (
    PARASITIC_VARIANT_GRAMMAR,
    body_terminal_flags,
    parse_parasitic_variants,
    star_hub_only_indices,
)
from test_extract import _make_inverter_layout, _write_gds


def _cards(report):
    return [
        ln.split()[0]
        for ln in Path(report["netlist_path"]).read_text().splitlines()
        if ln and ln[0] in "RCL"
    ]


def _variant_run(tmp_path, variants, *, layout=None, name="v", **kwargs):
    path = _write_gds(layout or _make_inverter_layout(), tmp_path / f"{name}.gds")
    return run_extract(
        path,
        "sky130",
        output=str(tmp_path / f"{name}.spice"),
        parasitics=True,
        parasitic_variants=variants,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Grammar / parsing
# ---------------------------------------------------------------------------


def test_parse_drop_r_body_verb_and_grammar_string():
    variants = parse_parasitic_variants(["drop-r-body:Y", "drop-r:Z"], ValueError)
    assert variants.drop_r_body == frozenset({"Y"})
    assert variants.drops_r_body("Y")
    assert not variants.drops_r_body("Z")
    assert not variants.drops_r("Y")
    assert variants.drops_any_r("Y")
    assert variants.drops_any_r("Z")
    assert variants.named_nets() == frozenset({"Y", "Z"})
    assert variants.raw == ("drop-r-body:Y", "drop-r:Z")
    wildcard = parse_parasitic_variants(["drop-r-body:*"], ValueError)
    assert wildcard.drops_r_body("anything")
    assert "drop-r-body:NET|*" in PARASITIC_VARIANT_GRAMMAR


def test_parse_rejects_bare_drop_r_body():
    with pytest.raises(ValueError, match="unknown --parasitic-variant"):
        parse_parasitic_variants(["drop-r-body"], ValueError)
    with pytest.raises(ValueError, match="unknown --parasitic-variant"):
        parse_parasitic_variants(["drop-r-body:"], ValueError)


def test_drop_r_body_requires_parasitics_and_rejects_unknown_verb():
    with pytest.raises(ExtractError, match="--parasitic-variant requires"):
        run_extract("/nonexistent.gds", "sky130", parasitic_variants=["drop-r-body:Y"])
    with pytest.raises(ExtractError, match="unknown --parasitic-variant"):
        run_extract(
            "/nonexistent.gds",
            "sky130",
            parasitics=True,
            parasitic_variants=["drop-r-body"],
        )


# ---------------------------------------------------------------------------
# Class-aware body classification (in-memory circuit, no GDS deck needed)
# ---------------------------------------------------------------------------


def _shared_net_circuit():
    """One net `N` carrying one terminal of each exotic device class:
    MOS4 (S G D B), BJT3 (C B E), BJT4 (C B E S), ResistorWithBulk (A B W),
    CapacitorWithBulk (A B W) -- so body-class terminals (`B`/`W`/`S`
    depending on class) share the net with plain signal terminals,
    including BJT3's `B`, which is a *base*, never a body."""
    netlist = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    # Hand-built nets all carry `cluster_id` 0 until a real connectivity
    # extraction assigns ids, and `_inject_parasitics` resolves entries via
    # a last-write-wins `{cluster_id: net}` dict -- so the ground net must
    # be created *before* `N` to keep the entry resolving to `N`.
    ground = circuit.create_net("0")
    net = circuit.create_net("N")
    class_specs = (
        (kdb.DeviceClassMOS4Transistor, "M4", ("S", "G", "D", "B")),
        (kdb.DeviceClassBJT3Transistor, "Q3", ("C", "B", "E")),
        (kdb.DeviceClassBJT4Transistor, "Q4", ("C", "B", "E", "S")),
        (kdb.DeviceClassResistorWithBulk, "RWB", ("A", "B", "W")),
        (kdb.DeviceClassCapacitorWithBulk, "CWB", ("A", "B", "W")),
    )
    for cls, name, terminals in class_specs:
        device_class = cls()
        device_class.name = f"{name}_class"
        netlist.add(device_class)
        device = circuit.create_device(device_class, f"{name}1")
        for terminal in terminals:
            device.connect_terminal(terminal, net)
    for cls, name, _parameters in (
        (kdb.DeviceClassResistor, "R", [("R", 1.0)]),
        (kdb.DeviceClassCapacitor, "C", [("C", 1e-15)]),
    ):
        device_class = cls()
        device_class.name = f"{name}_class"
        netlist.add(device_class)
        device = circuit.create_device(device_class, f"{name}1")
        device.connect_terminal("A", net)
        device.connect_terminal("B", ground)
        for parameter, value in _parameters:
            device.set_parameter(parameter, value)
    return netlist, circuit, net


def test_body_terminal_flags_are_class_aware():
    _netlist, _circuit, net = _shared_net_circuit()
    refs = list(net.each_terminal())
    flag_by = dict(
        zip(
            (f"{t.device().expanded_name()}/{t.terminal_def().name}" for t in refs),
            body_terminal_flags(refs),
            strict=True,
        )
    )
    assert flag_by["M41/B"]
    assert flag_by["RWB1/W"]
    assert flag_by["CWB1/W"]
    assert flag_by["Q41/S"]
    # BJT3's `B` is the base -- a signal terminal, never a body.
    assert not flag_by["Q31/B"]
    assert not flag_by["M41/S"]
    assert not flag_by["M41/G"]
    assert not flag_by["RWB1/A"]
    assert not flag_by["RWB1/B"]


def test_star_hub_only_indices_drop_r_wins_and_all_body_keeps_star():
    _netlist, _circuit, net = _shared_net_circuit()
    refs = list(net.each_terminal())
    body = parse_parasitic_variants(["drop-r-body:*"], ValueError)
    none = parse_parasitic_variants(["drop-r-body:NOPE"], ValueError)
    drop_r = parse_parasitic_variants(["drop-r:*"], ValueError)
    assert star_hub_only_indices(none, "N", False, refs) == frozenset()
    assert star_hub_only_indices(drop_r, "N", True, refs) == frozenset(range(len(refs)))
    hub_only = star_hub_only_indices(body, "N", False, refs)
    assert all(refs[i].terminal_def().name in ("B", "W", "S") for i in hub_only)
    assert "Q31" not in {refs[i].device().expanded_name() for i in hub_only}


def test_inject_parasitics_drop_r_body_on_shared_net():
    """`_inject_parasitics` level: body-class terminals stay on the hub with
    zero series R, signal terminals (BJT3 base included) keep legs, and the
    signal legs still sum back to the net's total R."""
    from klayout_tools.extract_parasitics import _inject_parasitics

    def inject(variants):
        netlist, circuit, net = _shared_net_circuit()
        report = _inject_parasitics(
            kdb,
            circuit,
            [
                {
                    "net_id": net.cluster_id,
                    "net": "N",
                    "resistance_ohm": 100.0,
                    "capacitance_ff": 2.0,
                }
            ],
            [],
            "0",
            variants=variants,
        )
        return report

    base = inject(None)
    dropped = inject(parse_parasitic_variants(["drop-r-body:N"], ValueError))
    base_entry = base["nets"][0]
    dropped_entry = dropped["nets"][0]
    assert base_entry["terminals"] != dropped_entry["terminals"]

    def hub_terms(entry):
        return {
            (t["device"], t["terminal"])
            for t in entry["terminals"]
            if t["leg_net"] == "N"
        }

    assert hub_terms(base_entry) == set()
    assert hub_terms(dropped_entry) == {
        ("M41", "B"),
        ("RWB1", "W"),
        ("CWB1", "W"),
        ("Q41", "S"),
    }
    # BJT3 base keeps a leg; signal legs sum back to the net total.
    signal_resistances = [
        t["resistance_ohm"] for t in dropped_entry["terminals"] if t["leg_net"] != "N"
    ]
    assert len(signal_resistances) == len(base_entry["terminals"]) - 4
    assert sum(signal_resistances) == pytest.approx(100.0, abs=0.01)
    # One series R per leg: 4 fewer cards than the all-terminal star.
    assert dropped["r_count"] == base["r_count"] - 4
    assert dropped["total_resistance_ohm"] == pytest.approx(
        base["total_resistance_ohm"]
    )
    assert dropped["c_count"] == base["c_count"]
    assert dropped["nets"][0]["capacitance_ff"] == pytest.approx(2.0)
    integrity = dropped["integrity"]
    assert integrity["nets_with_findings"] == 0
    assert integrity["orphan_nodes"] == 0


# ---------------------------------------------------------------------------
# CLI / report integration (sky130 inverter fixture)
# ---------------------------------------------------------------------------


def _net_by_name(report, name):
    return next(n for n in report["parasitics"]["nets"] if n["net"] == name)


def test_drop_r_body_cli_mixed_net_shorts_only_body_leaf(tmp_path, capsys):
    """The inverter fixture plus one li1 bridge that merges the NMOS source
    pad with a substrate tie labelled `VGND` makes VGND carry one signal
    (`S`) and one body (`B`) terminal -- the mixed net the variant exists
    for."""
    layout = _make_inverter_layout(substrate_tap_label="VGND")
    layout.top_cell().shapes(layout.layer(67, 20)).insert(kdb.Box(-450, -700, 400, 800))
    path = str(_write_gds(layout, tmp_path / "inv.gds"))
    base = _variant_run(tmp_path, None, layout=layout, name="a")
    dropped = _variant_run(tmp_path, ["drop-r-body:VGND"], layout=layout, name="b")

    by_terminal = {t["terminal"]: t for t in _net_by_name(dropped, "VGND")["terminals"]}
    assert by_terminal["B"]["leg_net"] == "VGND"
    assert by_terminal["B"]["resistance_ohm"] == 0.0
    assert by_terminal["S"]["leg_net"].startswith("VGND__t")
    # Single signal terminal: it now carries the net's whole total.
    assert by_terminal["S"]["resistance_ohm"] == pytest.approx(
        _net_by_name(dropped, "VGND")["resistance_ohm"], abs=1e-4
    )
    assert by_terminal["S"]["resistance_ohm"] + by_terminal["B"][
        "resistance_ohm"
    ] == pytest.approx(_net_by_name(dropped, "VGND")["resistance_ohm"], abs=1e-4)

    # Netlist: exactly one fewer VGND star R; everything else untouched.
    r_vgnd = re.compile(r"RVGND(_dup\d+)?_t\d+")
    assert len([c for c in _cards(base) if r_vgnd.fullmatch(c)]) == 2
    assert len([c for c in _cards(dropped) if r_vgnd.fullmatch(c)]) == 1
    assert "CVGND" in _cards(dropped)
    assert dropped["parasitics"]["r_count"] == base["parasitics"]["r_count"] - 1
    assert [c for c in _cards(base) if not r_vgnd.fullmatch(c)] == [
        c for c in _cards(dropped) if not r_vgnd.fullmatch(c)
    ]
    assert dropped["parasitics"]["parasitic_variant"] == ["drop-r-body:VGND"]
    assert dropped["warnings"] == base["warnings"]
    integrity = dropped["parasitics"]["integrity"]
    assert integrity["nets_with_findings"] == 0
    assert integrity["orphan_nodes"] == 0

    # CLI pass-through + JSON echo.
    code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "--parasitics",
            "--format",
            "json",
            "--parasitic-variant",
            "drop-r-body:VGND",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parasitics"]["parasitic_variant"] == ["drop-r-body:VGND"]


def test_drop_r_body_wildcard_leaves_all_body_nets_unchanged(tmp_path):
    """VPB/VTAP (a single body terminal each) and every signal-only net keep
    the exact star they had: an all-body net has no signal terminal to hand
    its resistance to, so the wildcard must not reshape it."""
    layout = _make_inverter_layout(substrate_tap_label="VTAP")
    base = _variant_run(tmp_path, None, layout=layout, name="a")
    dropped = _variant_run(tmp_path, ["drop-r-body:*"], layout=layout, name="b")

    def normalize(text, tag):
        return text.replace(f"{tag}.", "x.")

    assert normalize(Path(base["netlist_path"]).read_text(), "a") == normalize(
        Path(dropped["netlist_path"]).read_text(), "b"
    )
    vtap = _net_by_name(dropped, "VTAP")
    assert all(t["leg_net"].startswith("VTAP__t") for t in vtap["terminals"])
    assert vtap["terminals"] == _net_by_name(base, "VTAP")["terminals"]
    assert dropped["parasitics"]["r_count"] == base["parasitics"]["r_count"]
    assert dropped["parasitics"]["parasitic_variant"] == ["drop-r-body:*"]


def test_drop_r_body_absent_is_byte_identical(tmp_path):
    base = _variant_run(tmp_path, None, name="a")
    empty = _variant_run(tmp_path, [], name="b")
    assert "parasitic_variant" not in base["parasitics"]
    assert base["parasitics"] == empty["parasitics"]


def test_drop_r_body_unmatched_net_is_a_warning(tmp_path):
    report = _variant_run(tmp_path, ["drop-r-body:NOPE"], name="a")
    assert any("NOPE" in w and "--parasitic-variant" in w for w in report["warnings"])


def test_drop_r_body_leaves_distributed_rc_nets_unchanged_with_warning(tmp_path):
    """A `--distributed-rc` ladder named by `drop-r-body` is left unchanged
    and reported in `warnings`, the same refusal `drop-r` gets (issue
    #7317 edge case b)."""
    from test_extract import (
        _distributed_rc_internal_net_name,
        _make_series_nmos_layout,
    )

    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    net_name = _distributed_rc_internal_net_name(
        run_extract(
            path, "sky130", output=str(tmp_path / "probe.spice"), parasitics=True
        )
    )
    kwargs = dict(parasitics=True, critical_nets=[net_name], distributed_rc=True)
    base = run_extract(path, "sky130", output=str(tmp_path / "a.spice"), **kwargs)
    dropped = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "b.spice"),
        parasitic_variants=[f"drop-r-body:{net_name}"],
        **kwargs,
    )
    base_entry = next(n for n in base["parasitics"]["nets"] if n["net"] == net_name)
    dropped_entry = next(
        n for n in dropped["parasitics"]["nets"] if n["net"] == net_name
    )
    assert dropped_entry["rc_model"] == "distributed"
    assert dropped_entry["segments"] == base_entry["segments"]
    assert dropped_entry["terminals"] == base_entry["terminals"]
    assert any(
        "--parasitic-variant" in w
        and "does not apply" in w
        and "--distributed-rc" in w
        and repr(net_name) in w
        for w in dropped["warnings"]
    )
