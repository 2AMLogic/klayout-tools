"""DRC regressions for openings in standalone guard rings (#6477)."""

import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen import GenError, generate


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_gen.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Fabricate open_pdks variant directories for the PDK resolver."""
    root = tmp_path / "pdk_install"
    for variant in ("sky130A", "gf180mcuD"):
        (root / variant / "libs.tech").mkdir(parents=True)
    return root


def _gap_port(report):
    return next(p for p in report["ports"] if p["name"].startswith("GAP_"))


def _write_tech_lef(root, database_microns):
    techlef = root / "gf180mcuD" / "libs.ref" / "gf180mcu_fd_sc_mcu9t5v0" / "techlef"
    techlef.mkdir(parents=True)
    (techlef / "gf180mcu_fd_sc_mcu9t5v0__nom.tlef").write_text(
        f"UNITS\n  DATABASE MICRONS {database_microns} ;\nEND UNITS\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("width_um", [0.39, 0.4, 0.719])
def test_gf180_guard_ring_rejects_gap_below_implant_minimum(
    tmp_path, both_pdk_root, width_um
):
    output = tmp_path / "narrow_guard_ring.gds"
    with pytest.raises(
        GenError, match=r"ring_gap_um.*GF180MCU.*0\.72.*implant spacing"
    ):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
                "params": {"ring_gap_side": "S", "ring_gap_um": width_um},
                "options": {"output": str(output)},
            }
        )
    assert not output.exists()


def test_gf180_guard_ring_rejects_gap_that_rounds_below_minimum(
    tmp_path, both_pdk_root
):
    # At 128 DBU/µm the 0.72 µm request would draw a 92-DBU (0.71875 µm) cut.
    _write_tech_lef(both_pdk_root, 128)
    output = tmp_path / "rounded_gap.gds"
    with pytest.raises(GenError, match=r"0\.72.*after grid snapping"):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
                "params": {"ring_gap_side": "S", "ring_gap_um": 0.72},
                "options": {"output": str(output)},
            }
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("width_um", "side", "offset_um", "add_well", "comp_opening_um", "contact_count"),
    [
        (0.72, "S", 0.0, True, 0.72, 6),
        (0.72, "S", 0.0, False, 0.72, 6),
        (0.75, "S", 0.0, True, 0.75, 6),
        (0.80, "S", 0.0, True, 0.80, 6),
        # On E, the lower contact ends at 1.535 after grid snapping. The
        # first cut face snaps from 1.6035 to 1.600, leaving only 0.065 um.
        (0.72, "E", 0.0395, True, 0.725, 6),
        (0.72, "E", 0.041, True, 0.72, 7),  # exactly 0.07 um enclosure
    ],
)
def test_gf180_bare_guard_ring_opening_is_drc_clean(
    tmp_path,
    both_pdk_root,
    width_um,
    side,
    offset_um,
    add_well,
    comp_opening_um,
    contact_count,
):
    import klayout.db as kdb

    if side == "E":
        _write_tech_lef(both_pdk_root, 2000)
    output = tmp_path / "bare_guard_ring.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "inner_height_um": 3.008 if side == "E" else 3.0,
                "add_well": add_well,
                "ring_gap_side": side,
                "ring_gap_um": width_um,
                "ring_gap_offset_um": offset_um,
            },
            "options": {"output": str(output)},
        }
    )
    assert report["dbu_um"] == (0.0005 if side == "E" else 0.001)
    assert run_drc(str(output), "gf180mcu")["violations"] == []

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    for layer, expected_gap in (
        (22, comp_opening_um),  # Comp
        (34, comp_opening_um),  # Metal1
        (32 if add_well else 31, comp_opening_um - 0.32),  # Nplus/Pplus
    ):
        region = kdb.Region(top.begin_shapes_rec(layout.layer(layer, 0))).merged()
        assert region.count() == 1
        assert region.holes().is_empty()
        # Measure the missing span on a one-DBU strip at the opened edge.
        bbox = region.bbox()
        if side == "S":
            strip = kdb.Box(bbox.left, bbox.bottom, bbox.right, bbox.bottom + 1)
        else:
            strip = kdb.Box(bbox.right - 1, bbox.bottom, bbox.right, bbox.top)
        open_dbu = strip.area() - (region & kdb.Region(strip)).area()
        assert open_dbu * layout.dbu == pytest.approx(expected_gap)

    contacts = kdb.Region(top.begin_shapes_rec(layout.layer(33, 0)))
    assert report["device_count"] == contacts.count() == contact_count


def test_sky130_guard_ring_accepts_0_40_um_gap(tmp_path, both_pdk_root):
    output = tmp_path / "sky130_guard_ring.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "S",
                "ring_gap_um": 0.4,
            },
            "options": {"output": str(output)},
        }
    )
    assert _gap_port(report)["width_um"] == 0.4
    assert run_drc(str(output), "sky130")["violations"] == []


# --- sg13g2 mos_array: implant (pSD) opening across the ring cut (#6790) ---

#: The real-world IHP install's variant directory name (see test_gen.py's
#: own `_SG13G2_VARIANT`), not a "sg13g2"-prefixed string.
_SG13G2_VARIANT = "ihp-sg13g2"
_PSD_LAYER = (14, 0)  # pSD.drawing


@pytest.fixture()
def sg13g2_pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / _SG13G2_VARIANT / "libs.tech").mkdir(parents=True)
    return root


def _sg13g2_mos_array_request(root, output, **params):
    return {
        "generator": "mos_array",
        "pdk": {"variant": _SG13G2_VARIANT, "root": str(root)},
        "params": {"add_guard_ring": True, **params},
        "options": {"output": str(output)},
    }


def _psd_opening_um(output, side):
    """The drawn pSD opening on the ring's opened edge, on a one-DBU strip."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    region = kdb.Region(top.begin_shapes_rec(layout.layer(*_PSD_LAYER))).merged()
    bbox = region.bbox()
    if side == "S":
        strip = kdb.Box(bbox.left, bbox.bottom, bbox.right, bbox.bottom + 1)
    else:
        strip = kdb.Box(bbox.left, bbox.bottom, bbox.left + 1, bbox.top)
    open_dbu = strip.area() - (region & kdb.Region(strip)).area()
    return open_dbu * layout.dbu


@pytest.mark.parametrize(
    ("gap_um", "offset_um", "side", "drawn_open_um"),
    [
        (0.4, 0.0, "W", "0.040"),
        (0.6, 0.0, "W", "0.240"),
        (0.66, 0.0, "W", "0.300"),
        (0.665, 0.0025, "W", "0.305"),
        (0.4, 0.0, "S", "0.040"),  # a horizontal cut fails the same way
    ],
)
def test_sg13g2_mos_array_rejects_ring_gap_below_psd_minimum(
    tmp_path, sg13g2_pdk_root, gap_um, offset_um, side, drawn_open_um
):
    output = tmp_path / "psd_notch.gds"
    with pytest.raises(
        GenError,
        match=(
            rf"ring_gap_um={gap_um} on SG13G2 draws a {drawn_open_um} µm pSD "
            r"opening.*psd\.space\.1.*0\.31 µm.*increase ring_gap_um"
        ),
    ):
        generate(
            _sg13g2_mos_array_request(
                sg13g2_pdk_root,
                output,
                flavor="nfet",
                ring_gap_side=side,
                ring_gap_um=gap_um,
                ring_gap_offset_um=offset_um,
            )
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("gap_um", "offset_um", "expected_open_um"),
    [
        (0.665, 0.0, 0.31),  # exactly the drawn 0.31 µm boundary: accepted
        (0.67, 0.0, 0.31),
        (1.0, 0.0, 0.64),
    ],
)
def test_sg13g2_mos_array_accepts_ring_gap_at_psd_boundary(
    tmp_path, sg13g2_pdk_root, gap_um, offset_um, expected_open_um
):
    output = tmp_path / "psd_ok.gds"
    generate(
        _sg13g2_mos_array_request(
            sg13g2_pdk_root,
            output,
            flavor="nfet",
            ring_gap_side="W",
            ring_gap_um=gap_um,
            ring_gap_offset_um=offset_um,
        )
    )
    assert _psd_opening_um(output, "W") == pytest.approx(expected_open_um)
    assert run_drc(str(output), "sg13g2")["violations"] == []


def test_sg13g2_mos_array_ringless_gap_params_still_accepted(tmp_path, sg13g2_pdk_root):
    """No ring is drawn, so no pSD notch exists -- the opening request must
    not be rejected on the implant rule (#6790 control case)."""
    output = tmp_path / "ringless.gds"
    generate(
        _sg13g2_mos_array_request(
            sg13g2_pdk_root,
            output,
            flavor="nfet",
            add_guard_ring=False,
            ring_gap_side="W",
            ring_gap_um=0.4,
        )
    )
    assert run_drc(str(output), "sg13g2")["violations"] == []


def test_sg13g2_mos_array_pfet_ring_gap_still_accepted(tmp_path, sg13g2_pdk_root):
    """A pfet's ring resolves no pSD implant, so a 0.40 µm opening stays
    legal (#6790 control case)."""
    output = tmp_path / "pfet_ring.gds"
    generate(
        _sg13g2_mos_array_request(
            sg13g2_pdk_root,
            output,
            flavor="pfet",
            ring_gap_side="W",
            ring_gap_um=0.4,
        )
    )
    assert run_drc(str(output), "sg13g2")["violations"] == []


def test_sg13g2_mos_array_closed_ring_still_accepted(tmp_path, sg13g2_pdk_root):
    output = tmp_path / "closed_ring.gds"
    generate(_sg13g2_mos_array_request(sg13g2_pdk_root, output, flavor="nfet"))
    assert run_drc(str(output), "sg13g2")["violations"] == []


def test_sg13g2_standalone_guard_ring_gap_still_accepted(tmp_path, sg13g2_pdk_root):
    """The default (well-tie) standalone guard ring draws no pSD ring, so
    its 0.40 µm opening stays legal (#6790 control case)."""
    output = tmp_path / "standalone_ring.gds"
    generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
            "params": {"ring_gap_side": "W", "ring_gap_um": 0.4},
            "options": {"output": str(output)},
        }
    )
    assert run_drc(str(output), "sg13g2")["violations"] == []


def test_gf180mcu_mos_array_ring_gap_not_newly_rejected(tmp_path, both_pdk_root):
    """gf180mcu is out of #6790's scope: a request this issue's rule would
    reject on sg13g2 must keep its pre-issue behaviour there (accepted)."""
    output = tmp_path / "gf180_mos.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                "add_guard_ring": True,
                "ring_gap_side": "W",
                "ring_gap_um": 0.5,
            },
            "options": {"output": str(output)},
        }
    )
    assert output.exists()
