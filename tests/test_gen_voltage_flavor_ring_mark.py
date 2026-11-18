"""Regression tests for issue #6025: the ``voltage_flavor`` marker must cover
the well tie of a guard ring, so sky130's ``nwell.enclosed.hvi.1`` (nwell
touching ``hvi`` must be enclosed by ``hvi``) holds for a flavour-marked
device with a well tap ring -- both ``mos_array``'s own ``add_guard_ring`` and
a separately generated ``guard_ring``."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen import generate

_NWELL = (64, 20)
_HVI = (75, 20)

# `generate()` is handed the PDK variant plus a fabricated install root.
_VARIANTS = {
    "sky130": "sky130A",
    "gf180mcu": "gf180mcuD",
    "sg13g2": "ihp-sg13g2",
}
_FLAVOURS = {"sky130": "hvi", "gf180mcu": "medium_voltage", "sg13g2": "hv"}


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    for variant in _VARIANTS.values():
        (root / variant / "libs.tech").mkdir(parents=True)
    return root


def _gen(tmp_path, pdk_root, family, generator, params, name, cell_name="top"):
    output = tmp_path / f"{name}.gds"
    report = generate(
        {
            "generator": generator,
            "pdk": {"variant": _VARIANTS[family], "root": str(pdk_root)},
            "params": params,
            "options": {"output": str(output), "cell_name": cell_name},
        }
    )
    return output, report


def _region(path, layer_datatype):
    layout = kdb.Layout()
    layout.read(str(path))
    index = layout.find_layer(*layer_datatype)
    if index is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(index)).merged()


def _violations(path, deck="sky130"):
    return run_drc(str(path), deck)["rule_counts"]


_PFET_HVI = {"flavor": "pfet", "voltage_flavor": "hvi"}


def test_mos_array_hvi_ring_marker_encloses_ring_well_and_is_drc_clean(
    tmp_path, pdk_root
):
    """The issue's reproduction: one ``nwell.enclosed.hvi.1`` before the fix."""
    output, _ = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "mos_array",
        {
            "w_um": 2,
            "l_um": 10,
            "rows": 1,
            "cols": 1,
            "dummy": 0,
            "gate_contact": True,
            "add_guard_ring": True,
            **_PFET_HVI,
        },
        "ring_hvi",
        cell_name="mp",
    )
    assert (_region(output, _NWELL) - _region(output, _HVI)).is_empty()
    assert _violations(output) == {}


def test_mos_array_hvi_without_ring_marker_matches_well_box(tmp_path, pdk_root):
    """Ring-less requests are unchanged: marker box == the device well box."""
    output, _ = _gen(tmp_path, pdk_root, "sky130", "mos_array", _PFET_HVI, "noring")
    assert _region(output, _HVI).bbox() == _region(output, _NWELL).bbox()
    assert _violations(output) == {}


def test_mos_array_ring_without_flavour_draws_no_marker(tmp_path, pdk_root):
    output, _ = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "mos_array",
        {"flavor": "pfet", "add_guard_ring": True},
        "ring_noflavour",
    )
    assert _region(output, _HVI).is_empty()


def test_mos_array_nfet_ring_marker_not_widened(tmp_path, pdk_root):
    """An NMOS array's ring draws no well, so there is nothing to widen to."""
    ring_out, _ = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "mos_array",
        {"flavor": "nfet", "voltage_flavor": "hvi", "add_guard_ring": True},
        "nfet_ring",
    )
    plain_out, _ = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "mos_array",
        {"flavor": "nfet", "voltage_flavor": "hvi"},
        "nfet_plain",
    )
    assert _region(ring_out, _HVI).bbox() == _region(plain_out, _HVI).bbox()


def test_guard_ring_hvi_marks_well_and_is_drc_clean(tmp_path, pdk_root):
    output, report = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "guard_ring",
        {"inner_width_um": 6, "inner_height_um": 4, "voltage_flavor": "hvi"},
        "gr_hvi",
    )
    assert (_region(output, _HVI) ^ _region(output, _NWELL)).is_empty()
    assert not _region(output, _HVI).is_empty()
    assert report["drc_hints"]["voltage_flavor"] == "hvi"
    assert report["drc_hints"]["voltage_flavor_mark_present"] is True
    assert report["drc_hints"]["notes"] == []
    assert _violations(output) == {}


def test_guard_ring_default_output_unchanged(tmp_path, pdk_root):
    """``voltage_flavor`` defaults to ``""``: no marker, same geometry as a
    request that never mentions it."""
    implicit, report = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "guard_ring",
        {"inner_width_um": 6, "inner_height_um": 4},
        "gr_implicit",
    )
    explicit, _ = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "guard_ring",
        {"inner_width_um": 6, "inner_height_um": 4, "voltage_flavor": ""},
        "gr_explicit",
    )
    layouts = []
    for path in (implicit, explicit):
        layout = kdb.Layout()
        layout.read(str(path))
        layouts.append(layout)
    assert kdb.LayoutDiff().compare(layouts[0].top_cell(), layouts[1].top_cell())
    assert _region(implicit, _HVI).is_empty()
    assert report["drc_hints"]["voltage_flavor"] is None
    assert report["drc_hints"]["voltage_flavor_mark_present"] is False


def test_guard_ring_unrecognised_flavour_is_noted_not_dropped_silently(
    tmp_path, pdk_root
):
    output, report = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "guard_ring",
        {"voltage_flavor": "not_a_real_flavor"},
        "gr_unknown",
    )
    assert _region(output, _HVI).is_empty()
    assert report["drc_hints"]["voltage_flavor_mark_present"] is False
    assert any("voltage_flavor" in n for n in report["drc_hints"]["notes"])


def test_guard_ring_flavour_without_well_is_noted(tmp_path, pdk_root):
    """No well tie (``add_well`` false, e.g. a p-tap ring) means nothing to
    mark: reported via ``drc_hints.notes``, never silently dropped."""
    output, report = _gen(
        tmp_path,
        pdk_root,
        "sky130",
        "guard_ring",
        {"voltage_flavor": "hvi", "add_well": False},
        "gr_nowell",
    )
    assert _region(output, _HVI).is_empty()
    assert report["drc_hints"]["voltage_flavor_mark_present"] is False
    assert any("well" in n for n in report["drc_hints"]["notes"])


def test_guard_ring_abutting_flavour_marked_device_passes_nwell_enclosure(
    tmp_path, pdk_root
):
    """A separately generated ring around a flavour-marked, ring-less device:
    with the ring's own ``voltage_flavor`` the composite is clean; without it
    the ring's well is the unmarked nwell the deck rule flags."""
    dev, report = _gen(
        tmp_path, pdk_root, "sky130", "mos_array", _PFET_HVI, "dev", cell_name="dev"
    )
    box = report["bbox_um"]
    # Centre the device in the ring's 8x8 inner area; the offset is a whole
    # number of 5nm grid steps so the composite stays on grid.
    dx = round((4 - (box["x0"] + box["x1"]) / 2) * 200) * 5
    dy = round((4 - (box["y0"] + box["y1"]) / 2) * 200) * 5
    counts = {}
    for flavour in ("hvi", ""):
        ring, _ = _gen(
            tmp_path,
            pdk_root,
            "sky130",
            "guard_ring",
            {"inner_width_um": 8, "inner_height_um": 8, "voltage_flavor": flavour},
            f"ring_{flavour or 'plain'}",
            cell_name="ring",
        )
        layout = kdb.Layout()
        layout.read(str(ring))
        device_layout = kdb.Layout()
        device_layout.read(str(dev))
        ring_top = layout.top_cell()
        device_cell = layout.create_cell("dev")
        device_cell.copy_tree(device_layout.top_cell())
        ring_top.insert(
            kdb.CellInstArray(device_cell.cell_index(), kdb.Trans(kdb.Vector(dx, dy)))
        )
        composite = tmp_path / f"composite_{flavour or 'plain'}.gds"
        layout.write(str(composite))
        counts[flavour] = _violations(composite)
    assert counts["hvi"] == {}
    assert counts[""].get("nwell.enclosed.hvi.1", 0) >= 1


@pytest.mark.parametrize("family", ["gf180mcu", "sg13g2"])
@pytest.mark.parametrize("flavor", ["pfet", "nfet"])
def test_other_families_flavour_with_ring_stay_drc_clean(
    tmp_path, pdk_root, family, flavor
):
    """gf180mcu ``medium_voltage`` / sg13g2 ``hv`` ring variants: the PMOS
    ring's marker is widened over its well tie like sky130's, and every
    variant stays clean on the curated deck."""
    output, _ = _gen(
        tmp_path,
        pdk_root,
        family,
        "mos_array",
        {
            "flavor": flavor,
            "voltage_flavor": _FLAVOURS[family],
            "add_guard_ring": True,
        },
        f"{family}_{flavor}_ring",
    )
    assert _violations(output, family) == {}
