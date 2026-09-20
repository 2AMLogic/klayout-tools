"""Cross-validation of `klt extract` against **magic**'s `extract` ->
`ext2spice` flow -- issue #2014, pairing #1 of tracking issue #2007.

This is the half of #2007 that had no coverage at all. `tests/test_lvs.py`
already runs netgen against KLayout as an independent *comparator*, but
both sides of that comparison are fed by KLayout's own extraction, so
extraction itself was single-implementation: nothing in the suite could
tell a correct netlist from a consistently-wrong one. magic extracts from
the same GDS with its own geometry engine and its own open_pdks device
recognition, so the device list, the parameters and the connectivity it
produces are an independent answer to the same question.

Gating, methodology, and the declared shared surface are documented in
`tests/test_drc_magic_oracle.py`'s module docstring and, in full, in
`docs/design/magic-oracle.md`. In short: real-binary-gated (skips cleanly
without `magic` or a magic technology file), both engines read the same
GDS bytes, and magic's loaded-cell bbox is asserted against KLayout's
before any verdict is compared.

## What agreement means here

Unlike DRC -- where the two engines' *reporting* conventions differ (see
the DRC module) -- extraction results are directly comparable, and this
module asserts exact agreement on:

- device count and per-class counts (`nfet`/`pfet`);
- every device parameter `klt` reports: `w`, `l`, `as`, `ad`, `ps`, `pd`,
  in µm/µm², after normalising magic's optional SI suffixes;
- terminal connectivity by net *name* for drain/gate/source;
- total net count.

Two naming differences are declared, not compared (they are conventions,
not verdicts): magic names a MOSFET's bulk from its own well/substrate
node (`VNB`, `SUB`, `w_n86_453#`) where `klt` synthesises `vsubs` or
numbers the well net, and magic generates `a_<x>_<y>#`-style names for
unlabelled internal nets where `klt` uses `$<n>`. Both are covered in
`docs/design/magic-oracle.md`.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from helpers import magic_oracle
from helpers.magic_oracle import MagicDevice, run_magic_extract
from klayout_tools.extract import run_extract

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_INV = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
GF180_CLKINV = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

#: magic writes a MOSFET's subcircuit terminals in this order for both
#: PDKs' open_pdks models (verified against real `ext2spice lvs` output
#: for `sky130_fd_pr__nfet_01v8`/`pfet_01v8_hvt` and `nfet_05v0`/
#: `pfet_05v0`). A PDK whose model declared a different port order would
#: fail these tests loudly rather than silently -- this constant is where
#: it would be taught.
MAGIC_MOSFET_TERMINALS = ("d", "g", "s", "b")

#: `klt extract`'s JSON parameter name for each `ext2spice` parameter.
PARAM_KEYS = {
    "l": "l_um",
    "w": "w_um",
    "as": "as_um2",
    "ad": "ad_um2",
    "ps": "ps_um",
    "pd": "pd_um",
}


def _require_oracle(deck: str) -> None:
    reason = magic_oracle.oracle_skip_reason(deck)
    if reason:
        pytest.skip(reason)


@pytest.fixture
def sky130_oracle() -> str:
    _require_oracle("sky130")
    return "sky130"


@pytest.fixture
def gf180mcu_oracle() -> str:
    _require_oracle("gf180mcu")
    return "gf180mcu"


def _top_cell(path: Path | str) -> str:
    layout = kdb.Layout()
    layout.read(str(path))
    return layout.top_cell().name


def _bbox_um(path: Path | str) -> tuple[float, float, float, float]:
    layout = kdb.Layout()
    layout.read(str(path))
    box = layout.top_cell().bbox()
    dbu = layout.dbu
    return (
        round(box.left * dbu, 4),
        round(box.bottom * dbu, 4),
        round(box.right * dbu, 4),
        round(box.top * dbu, 4),
    )


def _klt_signatures(report: dict) -> list[tuple]:
    """`(class, l, w, as, ad, ps, pd)` per device, sorted -- the
    order-independent form the two device lists are compared in."""
    signatures = []
    for device in report["devices"]:
        params = device["params"]
        signatures.append(
            (device["class"],)
            + tuple(round(float(params[key]), 4) for key in PARAM_KEYS.values())
        )
    return sorted(signatures)


def _magic_signatures(devices: tuple[MagicDevice, ...]) -> list[tuple]:
    signatures = []
    for device in devices:
        signatures.append(
            (device.kind,)
            + tuple(round(float(device.params[key]), 4) for key in PARAM_KEYS)
        )
    return sorted(signatures)


def _klt_terminals(report: dict) -> list[tuple]:
    """`(class, drain, gate, source)` per device, sorted. Bulk is excluded
    -- see the module docstring's declared naming differences."""
    return sorted(
        (device["class"], device["nets"]["d"], device["nets"]["g"], device["nets"]["s"])
        for device in report["devices"]
    )


def _magic_terminals(devices: tuple[MagicDevice, ...]) -> list[tuple]:
    rows = []
    for device in devices:
        assert len(device.terminals) == len(MAGIC_MOSFET_TERMINALS), device
        by_role = dict(zip(MAGIC_MOSFET_TERMINALS, device.terminals, strict=True))
        rows.append((device.kind, by_role["d"], by_role["g"], by_role["s"]))
    return sorted(rows)


def _seed_net_short(source: Path, dest: Path) -> None:
    """Copy ``source``, adding an li1 bridge that shorts the inverter's
    input to its output (the `A`-labelled li1 island to the `Y` one).

    A net short is the extraction-side counterpart of the DRC module's
    seeded spacing error: it changes *connectivity*, which is exactly what
    an extractor is for, and it is visible to both engines as one fewer
    net without either having to agree on what to call the merged net.
    """
    layout = kdb.Layout()
    layout.read(str(source))
    top = layout.top_cell()
    li1 = layout.layer(67, 20)
    top.shapes(li1).insert(kdb.Box(640, 1100, 830, 1300))
    layout.write(str(dest))


def _seed_missing_vias(source: Path, dest: Path) -> int:
    """Copy ``source`` with every `licon1` cut (66/44) deleted -- the
    "missing via" defect #2007's criterion 3 names, in its most
    unambiguous form: the contacts between diffusion/poly and local
    interconnect are simply gone, so every device terminal falls onto its
    own isolated island. Returns how many cuts were removed."""
    layout = kdb.Layout()
    layout.read(str(source))
    top = layout.top_cell()
    licon1 = layout.layer(66, 44)
    removed = top.shapes(licon1).size()
    top.shapes(licon1).clear()
    layout.write(str(dest))
    return removed


# --------------------------------------------------------------------------- #
# Known-good case: a real corpus cell both extractors must agree on
# --------------------------------------------------------------------------- #


def test_sky130_clean_inverter_devices_and_nets_agree(sky130_oracle, tmp_path):
    """#2007 criterion 3's known-good half: same GDS, same device list,
    same net count, from two independent extractors."""
    cell = _top_cell(SKY130_INV)
    report = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    oracle = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    assert oracle.cell_bbox_um == _bbox_um(SKY130_INV)
    assert oracle.tech_name == "sky130A"
    assert oracle.top == report["top"] == cell

    assert oracle.device_count == report["device_count"] == 2
    assert oracle.device_counts == report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert oracle.net_count == report["net_count"] == 6


def test_sky130_clean_inverter_device_parameters_agree(sky130_oracle, tmp_path):
    """Beyond counts: every geometric device parameter `klt extract`
    reports -- channel `w`/`l` and the source/drain areas and perimeters
    -- is computed independently by magic from the same polygons and must
    match exactly."""
    cell = _top_cell(SKY130_INV)
    report = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    oracle = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    assert _magic_signatures(oracle.devices) == _klt_signatures(report)
    # Evidence the parameters compared are real, not a pair of empty dicts.
    assert _klt_signatures(report)[0][1:] == (0.15, 0.65, 0.169, 0.169, 1.82, 1.82)


def test_sky130_clean_inverter_terminal_nets_agree(sky130_oracle, tmp_path):
    """Connectivity, not just counts: each device's drain/gate/source land
    on the same named nets in both extractions."""
    cell = _top_cell(SKY130_INV)
    report = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    oracle = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    assert _magic_terminals(oracle.devices) == _klt_terminals(report)
    assert _klt_terminals(report) == [
        ("nfet", "Y", "A", "VGND"),
        ("pfet", "Y", "A", "VPWR"),
    ]


def test_gf180mcu_clean_clkinv_agrees_with_magic(gf180mcu_oracle, tmp_path):
    """The same agreement one PDK over, against magic's independently
    transcribed `gf180mcuC` extraction rules."""
    cell = _top_cell(GF180_CLKINV)
    report = run_extract(
        str(GF180_CLKINV), gf180mcu_oracle, output=str(tmp_path / "klt.spice")
    )
    oracle = run_magic_extract(
        str(GF180_CLKINV), deck=gf180mcu_oracle, cell=cell, work_dir=tmp_path
    )

    assert oracle.cell_bbox_um == _bbox_um(GF180_CLKINV)
    assert oracle.tech_name == "gf180mcuC"

    assert oracle.device_count == report["device_count"] == 2
    assert oracle.device_counts == report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert oracle.net_count == report["net_count"] == 6
    assert _magic_signatures(oracle.devices) == _klt_signatures(report)
    assert _magic_terminals(oracle.devices) == _klt_terminals(report)


# --------------------------------------------------------------------------- #
# Seeded defects: both extractors must react identically
# --------------------------------------------------------------------------- #


def test_seeded_short_collapses_one_net_in_both_extractors(sky130_oracle, tmp_path):
    """#2007 criterion 3's load-bearing half for extraction: an injected
    input-to-output short must cost both extractors exactly one net, and
    both must show the surviving device's gate and drain on the *same*
    net.

    The gate/drain assertion is deliberately structural rather than
    name-based: `klt` reports the merged net as `A|Y` and magic as `Y`, so
    only the topology is comparable -- and the topology is the verdict.
    """
    shorted = tmp_path / "inv_short.gds"
    _seed_net_short(SKY130_INV, shorted)
    cell = _top_cell(shorted)

    clean = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt_clean.spice")
    )
    clean_oracle = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path / "clean"
    )
    report = run_extract(
        str(shorted), sky130_oracle, output=str(tmp_path / "klt_short.spice")
    )
    oracle = run_magic_extract(
        str(shorted), deck=sky130_oracle, cell=cell, work_dir=tmp_path / "short"
    )

    assert report["net_count"] == clean["net_count"] - 1
    assert oracle.net_count == clean_oracle.net_count - 1
    assert report["net_count"] == oracle.net_count

    # Devices survive the short in both -- the defect merged nets, it did
    # not lose a transistor.
    assert report["device_count"] == oracle.device_count == 2

    for device in report["devices"]:
        assert device["nets"]["g"] == device["nets"]["d"], device
    for magic_device in oracle.devices:
        by_role = dict(zip(MAGIC_MOSFET_TERMINALS, magic_device.terminals, strict=True))
        assert by_role["g"] == by_role["d"], magic_device


def test_seeded_missing_vias_split_the_same_nets_in_both_extractors(
    sky130_oracle, tmp_path
):
    """The other defect shape #2007 names: a missing via. With every
    `licon1` cut deleted, both extractors must report the *same* larger
    net count -- each device terminal isolated on its own island -- and
    both must still find both transistors."""
    broken = tmp_path / "inv_missing_licon.gds"
    removed = _seed_missing_vias(SKY130_INV, broken)
    assert removed == 11, "fixture drifted: expected 11 licon1 cuts in this cell"
    cell = _top_cell(broken)

    clean = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt_clean.spice")
    )
    report = run_extract(
        str(broken), sky130_oracle, output=str(tmp_path / "klt_broken.spice")
    )
    oracle = run_magic_extract(
        str(broken), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    assert report["net_count"] > clean["net_count"]
    assert report["net_count"] == oracle.net_count
    assert report["device_count"] == oracle.device_count == 2
    # The devices themselves are unchanged geometry -- only their
    # connectivity broke -- so both extractors must still agree on every
    # device parameter.
    assert _magic_signatures(oracle.devices) == _klt_signatures(report)


# --------------------------------------------------------------------------- #
# Provenance (#2007 criterion 4)
# --------------------------------------------------------------------------- #


def test_extract_oracle_provenance_records_versions_and_the_same_input_bytes(
    sky130_oracle, tmp_path
):
    cell = _top_cell(SKY130_INV)
    report = run_extract(
        str(SKY130_INV), sky130_oracle, output=str(tmp_path / "klt.spice")
    )
    oracle = run_magic_extract(
        str(SKY130_INV), deck=sky130_oracle, cell=cell, work_dir=tmp_path
    )

    provenance = magic_oracle.oracle_provenance(
        deck=sky130_oracle,
        gds_path=str(SKY130_INV),
        klt_report=report,
        tech_version=oracle.tech_version,
    )

    assert provenance["oracle"]["version"] == magic_oracle.magic_version()
    assert provenance["oracle"]["deck"]["content_hash"].startswith("sha256:")
    assert provenance["klt"]["deck"]["name"] == "sky130"
    assert (
        provenance["klt"]["input"]["content_hash"]
        == provenance["input"]["content_hash"]
    )
    # The extracted netlist itself is hashed by `klt` too, so a recorded
    # comparison can be re-run against the exact netlist that was compared.
    assert report["netlist_sha256"]
