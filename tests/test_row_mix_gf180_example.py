"""Tests for `examples/row-mix-gf180/` (issue #3969): the generic mixed-device,
shared-diffusion, fixed-pitch layout authored from the custom-transistor-layout
study's section 4.1.

Covers the study's section 4.2 oracle (determinism, device/connectivity
equivalence independent of the generator, supply continuity, pitch, ports) and
the section 4.3 failure cases that need no new engine code. Case 4.3(5)'s
native-engine half needs a standalone KLayout run and is deliberately not run
here -- see the example README.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "row-mix-gf180"
REFERENCE = EXAMPLE_DIR / "row_mix_gf180.sp"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "_row_mix_gf180_gen", EXAMPLE_DIR / "generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _write(layout: kdb.Layout, path: Path) -> Path:
    gen.write_gds(layout, str(path))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def cell_gds(tmp_path_factory) -> Path:
    return _write(gen.build_layout(), tmp_path_factory.mktemp("rowmix") / "cell.gds")


@pytest.fixture(scope="module")
def extraction(cell_gds, tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("rowmix_ext") / "cell.spice"
    return run_extract(str(cell_gds), "gf180mcu", output=str(out))


def _lvs(gds: Path, reference: Path, tmp_path: Path) -> dict:
    request = tmp_path / "lvs.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": str(gds), "deck": "gf180mcu"},
                "reference": {"netlist": str(reference)},
            }
        )
    )
    return run_lvs(str(request))


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_two_runs_give_identical_gds_sha256(tmp_path):
    first = _write(gen.build_layout(), tmp_path / "a.gds")
    second = _write(gen.build_layout(), tmp_path / "b.gds")
    assert _sha256(first) == _sha256(second)


def test_committed_gds_matches_regeneration(tmp_path):
    committed = EXAMPLE_DIR / "row_mix_gf180.gds"
    assert _sha256(_write(gen.build_layout(), tmp_path / "regen.gds")) == _sha256(
        committed
    )
    pair = EXAMPLE_DIR / "row_mix_gf180_pair.gds"
    assert _sha256(_write(gen.build_pair(), tmp_path / "pair.gds")) == _sha256(pair)


# --------------------------------------------------------------------------- #
# Device / connectivity equivalence, independent of the generator
# --------------------------------------------------------------------------- #
def test_lvs_matches_independent_reference(cell_gds, tmp_path):
    report = _lvs(cell_gds, REFERENCE, tmp_path)
    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 14, "reference": 14, "matched": 14}
    assert report["counts"]["nets"]["matched"] == 13
    errors = [m for m in report["mismatches"] if m["severity"] == "error"]
    assert errors == []


def _reference_devices() -> list[tuple[str, float, float]]:
    out = []
    for line in REFERENCE.read_text().splitlines():
        m = re.match(
            r"M\w+ .* (nfet|pfet) L=([\d.]+)U W=([\d.]+)U", line, re.IGNORECASE
        )
        if m:
            out.append((m.group(1).lower(), float(m.group(2)), float(m.group(3))))
    return out


def test_extracted_device_sizes_within_one_dbu_of_reference(extraction):
    extracted = sorted(
        (d["class"], d["params"]["l_um"], d["params"]["w_um"])
        for d in extraction["devices"]
    )
    reference = sorted(_reference_devices())
    assert len(extracted) == len(reference) == 14
    for (cls_e, l_e, w_e), (cls_r, l_r, w_r) in zip(extracted, reference, strict=True):
        assert cls_e == cls_r
        assert abs(l_e - l_r) <= gen.DBU_UM + 1e-9
        assert abs(w_e - w_r) <= gen.DBU_UM + 1e-9


def test_lvs_rejects_a_one_dbu_width_change_in_the_reference(cell_gds, tmp_path):
    tampered = tmp_path / "tampered.sp"
    tampered.write_text(
        REFERENCE.read_text().replace(
            "MN1 n1 A VSS VSS nfet L=0.28U W=0.8U",
            "MN1 n1 A VSS VSS nfet L=0.28U W=0.801U",
        )
    )
    assert tampered.read_text() != REFERENCE.read_text()
    assert _lvs(cell_gds, tampered, tmp_path)["status"] != "match"


def test_shared_diffusion_chain_has_no_contact_on_internal_series_nodes(extraction):
    # p1/p2 (NOR3 pull-up series nodes) exist only as shared diffusion.
    anonymous = [n for n in extraction["nets"] if "$" in n["name"]]
    # p1/p2 each join exactly two devices and no pin: one diffusion shape, no metal.
    assert sum(1 for n in anonymous if n["device_count"] == 2) == 2
    assert extraction["device_count"] == 14


# --------------------------------------------------------------------------- #
# Supply continuity, ports, pitch, abutment
# --------------------------------------------------------------------------- #
def test_supplies_are_single_nets_with_no_split_labels(extraction):
    names = [n["name"] for n in extraction["nets"]]
    assert names.count("VSS") == 1
    assert names.count("VDD") == 1
    assert extraction["split_net_labels"] == []


def test_omitting_a_vss_strap_breaks_supply_continuity(tmp_path):
    gds = _write(gen.build_layout(strap_vss=False), tmp_path / "unstrapped.gds")
    assert _lvs(gds, REFERENCE, tmp_path)["status"] != "match"


def test_ports_sit_at_requested_x_including_the_off_grid_one(extraction):
    positions = {
        n["name"]: n["label_positions_um"][0]["x_um"]
        for n in extraction["nets"]
        if n.get("label_positions_um")
    }
    for name, x in gen.PORT_X.items():
        assert positions[name] == pytest.approx(x, abs=1e-9)
    grid = gen.ROUTING_GRID_X
    off_grid = [
        n for n, x in gen.PORT_X.items() if abs(x / grid - round(x / grid)) > 1e-6
    ]
    assert off_grid == ["B"]


def test_cell_outline_width_equals_requested_pitch(cell_gds):
    layout = kdb.Layout()
    layout.read(str(cell_gds))
    width_um = layout.top_cell().bbox().width() * layout.dbu
    assert width_um == pytest.approx(gen.PITCH_UM, abs=1e-9)
    assert gen.PITCH_UM < gen.isolated_width_um(
        gen.DEVICES
    )  # sharing is required to fit


def test_mirrored_pair_abuts_with_shared_rails_and_continuing_control_net(tmp_path):
    gds = _write(gen.build_pair(), tmp_path / "pair.gds")
    report = run_extract(str(gds), "gf180mcu", output=str(tmp_path / "pair.spice"))
    names = [n["name"] for n in report["nets"]]
    assert report["device_count"] == 28
    for shared in ("VSS", "VDD", "S"):  # one net across the seam, not one per copy
        assert names.count(shared) == 1
    assert report["split_net_labels"] == []
    drc = run_drc(str(gds), "gf180mcu")
    assert drc["status"] == "clean"


# --------------------------------------------------------------------------- #
# DRC (curated deck) and its disclosed limits
# --------------------------------------------------------------------------- #
def test_curated_drc_clean_and_unmodeled_rules_disclosed(cell_gds):
    report = run_drc(str(cell_gds), "gf180mcu")
    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    coverage = report["coverage"]
    assert len(coverage["rules_checked"]) > 0
    assert coverage["unmodeled_rules"], (
        "a clean verdict must carry the unmodeled-rule disclosure"
    )


def test_curated_deck_does_not_flag_wide_metal_at_minimum_spacing(tmp_path):
    """Study 4.3(5), curated half: two wide Metal1 edges at the deck's single
    spacing threshold. The curated deck has no wide-metal spacing rule, so it
    reports clean; whether the native deck flags it is NOT run here."""
    layout = kdb.Layout()
    layout.dbu = gen.DBU_UM
    top = layout.create_cell("wide")
    m1 = layout.layer(*gen.M1)
    top.shapes(m1).insert(kdb.Box(0, 0, 12000, 12000))
    top.shapes(m1).insert(kdb.Box(0, 12230, 12000, 24230))
    gds = _write(layout, tmp_path / "wide.gds")
    report = run_drc(str(gds), "gf180mcu")
    assert report["status"] == "clean"
    assert "metal1.space.1" not in report["rule_counts"]


# --------------------------------------------------------------------------- #
# Study 4.3 failure cases that need no new engine code
# --------------------------------------------------------------------------- #
def test_facing_net_mismatch_is_a_hard_error_naming_the_pair():
    chains = {
        "n": ["MN1", "MN3a", gen.BREAK, "MN2", "MN3b", "MN3c", gen.BREAK, "MN4", "MN5"],
        "p": gen.CHAINS["p"],
    }
    with pytest.raises(gen.RowError) as err:
        gen.build_layout(chains_spec=chains)
    assert err.value.code == "facing_net_mismatch"
    assert err.value.detail["devices"] == ["MN1", "MN3a"]


def test_pitch_below_minimum_reports_the_deficit_in_dbu():
    with pytest.raises(gen.RowError) as err:
        gen.build_layout(pitch_um=7.0)
    assert err.value.code == "pitch_below_minimum"
    assert err.value.detail["deficit_dbu"] == 840  # 7.84 um minimum - 7.0 um


def test_ports_closer_than_metal_pitch_are_rejected_naming_both():
    with pytest.raises(gen.RowError) as err:
        gen.build_layout(port_x={"A": 0.80, "B": 1.00})
    assert err.value.code == "ports_too_close"
    assert err.value.detail["ports"] == ["A", "B"]


def test_netlist_device_without_row_assignment_is_an_error_not_a_drop():
    chains = {
        "n": [d for d in gen.CHAINS["n"] if d != "MN5"],
        "p": gen.CHAINS["p"],
    }
    with pytest.raises(gen.RowError) as err:
        gen.build_layout(chains_spec=chains)
    assert err.value.code == "unassigned_device"
    assert err.value.detail["devices"] == ["MN5"]
