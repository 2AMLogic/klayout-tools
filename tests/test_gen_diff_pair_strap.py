"""Tests for `diff_pair`'s `strap_splits` (issue #5557).

Split out of `test_gen.py` (file-size ratchet). The strapped cell ties each
matched device's `splits` sub-instances together in-cell on met1 (sky130's
metal2/mcon roles) and reports one S/D/G port triple per device, so the
generator's port list matches the netlist's device list -- verified here by
DRC-clean runs against the curated sky130 deck and by extracted-netlist
net identity, mirroring `test_gen.py`'s own diff_pair coverage.
"""

import itertools
import re

import pytest

from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen import GenError, generate, list_generators

#: The strapped report's device-level port order: device 1 first, S/D/G
#: within a device (issue #5557 acceptance criterion).
_DEVICE_PORTS = ["Q1_S", "Q1_D", "Q1_G", "Q2_S", "Q2_D", "Q2_G"]


def _make_install(root, variant="sky130A"):
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True, exist_ok=True)
    return variant_dir


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root)
    return root


def _strap_request(
    tmp_path, pdk_root, params: dict, name="dp_strap", cell_name=None
) -> dict:
    return {
        "generator": "diff_pair",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "params": {
            "w_um": 3.0,
            "l_um": 0.5,
            "splits": 2,
            "gate_contact": True,
            "add_guard_ring": False,
            **params,
        },
        "options": {
            "cell_name": cell_name or name,
            "output": str(tmp_path / f"{name}.gds"),
        },
    }


def _device_ports(report):
    return [p["name"] for p in report["ports"] if not p["name"].startswith("TAP_")]


# --- the default is a no-op ---------------------------------------------------


def test_strap_splits_default_leaves_output_unchanged(tmp_path, pdk_root):
    """`strap_splits` omitted vs explicitly false must produce identical
    geometry and identical ports -- the default is byte-for-byte today's
    unstrapped cell (issue #5557 acceptance criterion)."""
    omitted = generate(
        _strap_request(tmp_path, pdk_root, {}, "omitted", cell_name="dp_same")
    )
    explicit = generate(
        _strap_request(
            tmp_path, pdk_root, {"strap_splits": False}, "explicit", cell_name="dp_same"
        )
    )
    assert _device_ports(omitted) == _device_ports(explicit)
    assert omitted["ports"] == explicit["ports"]
    assert omitted["bbox_um"] == explicit["bbox_um"]

    # Geometry-identical too (same cell name so LayoutDiff's tree compare
    # cannot trip on it -- mirrors test_gen.py's own `_assert_gds_geometry_equal`).
    import klayout.db as kdb

    layout_a = kdb.Layout()
    layout_a.read(str(tmp_path / "omitted.gds"))
    layout_b = kdb.Layout()
    layout_b.read(str(tmp_path / "explicit.gds"))
    assert kdb.LayoutDiff().compare(layout_a.top_cell(), layout_b.top_cell())


# --- the strapped report -------------------------------------------------------


@pytest.mark.parametrize("splits", [2, 3, 8])
def test_strapped_report_carries_one_port_triple_per_device(tmp_path, pdk_root, splits):
    """With `strap_splits: true` the report carries exactly the six
    device-level ports, device 1 first, with no per-sub-instance
    `Q<d>_<n>_*` ports; `device_count` stays `2 * splits` and the guard
    ring's TAP ports are untouched."""
    report = generate(
        _strap_request(
            tmp_path, pdk_root, {"strap_splits": True, "splits": splits}, f"dp{splits}"
        )
    )
    assert report["device_count"] == 2 * splits
    assert _device_ports(report) == _DEVICE_PORTS
    assert not any(re.match(r"Q\d+_\d+_", p["name"]) for p in report["ports"])
    met1 = {"layer": 68, "datatype": 20, "name": None}
    for p in report["ports"]:
        if p["name"] in _DEVICE_PORTS:
            assert p["layer"] == met1, f"{p['name']} must report the strap metal"


def test_strapped_ports_sit_on_drawn_strap_metal(tmp_path, pdk_root):
    """Every reported device port lies on drawn met1 geometry of its strap
    rail (issue #5557: the port must be landable on the layer it reports)."""
    report = generate(
        _strap_request(tmp_path, pdk_root, {"strap_splits": True}, "dp_on_metal")
    )
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(tmp_path / "dp_on_metal.gds"))
    met1 = layout.layer(68, 20)
    dbu = layout.dbu
    for p in report["ports"]:
        if p["name"] not in _DEVICE_PORTS:
            continue
        point = kdb.Point(int(round(p["x_um"] / dbu)), int(round(p["y_um"] / dbu)))
        on_met1 = False
        it = layout.top_cell().begin_shapes_rec(met1)
        while not it.at_end():
            if it.shape().bbox().contains(point):
                on_met1 = True
                break
            it.next()
        assert on_met1, (
            f"port {p['name']} at ({p['x_um']}, {p['y_um']}) is not on drawn met1"
        )


def test_strapped_mirror_naming(tmp_path, pdk_root):
    """`mirror: true` keeps the M1/M2 prefixes on the strapped ports."""
    report = generate(
        _strap_request(
            tmp_path, pdk_root, {"strap_splits": True, "mirror": True}, "dp_mirror"
        )
    )
    assert _device_ports(report) == [
        "M1_S",
        "M1_D",
        "M1_G",
        "M2_S",
        "M2_D",
        "M2_G",
    ]


def test_strapped_splits_one_still_reports_device_level_names(tmp_path, pdk_root):
    """`splits=1` has nothing to interleave (each device is a single unit in
    opposite rows) but the strap must still report the same `Q<d>_<t>`
    device-level names -- the port list's shape is the contract, not the
    split count."""
    report = generate(
        _strap_request(tmp_path, pdk_root, {"strap_splits": True, "splits": 1}, "dp1")
    )
    assert _device_ports(report) == _DEVICE_PORTS
    assert report["device_count"] == 2


def test_gen_list_documents_strap_splits():
    """`klt gen --list` surfaces the new param (it is the caller's
    discovery surface, mirroring `splits`' own #1495 coverage)."""
    report = list_generators()
    generator = next(g for g in report["generators"] if g["name"] == "diff_pair")
    strap = next(p for p in generator["params"] if p["name"] == "strap_splits")
    assert "strap" in strap["description"].lower()


# --- DRC + extraction (issue #5557's acceptance matrix) ------------------------


@pytest.mark.parametrize(
    ("splits", "ring", "flavor"),
    list(itertools.product((2, 3, 8), (False, True), ("nfet", "pfet"))),
)
def test_strapped_cell_is_drc_clean_and_extracts_two_legs(
    tmp_path, pdk_root, splits, ring, flavor
):
    """The full acceptance matrix: for `splits` in {2, 3, 8} (even, odd and
    the reported case), nfet and pfet, with and without the auto-sized
    guard ring, the strapped cell is DRC clean under the curated sky130
    deck and extracts with every device of each leg sharing one source,
    one drain and one gate net -- six pairwise-distinct nets, no shorts,
    no extra floating nodes."""
    name = f"dp_{splits}_{int(ring)}_{flavor}"
    generate(
        _strap_request(
            tmp_path,
            pdk_root,
            {
                "strap_splits": True,
                "splits": splits,
                "add_guard_ring": ring,
                "flavor": flavor,
            },
            name,
        )
    )
    drc_report = run_drc(str(tmp_path / f"{name}.gds"), "sky130")
    assert drc_report["status"] == "clean", drc_report["rule_counts"]

    extract_report = run_extract(str(tmp_path / f"{name}.gds"), "sky130")
    assert extract_report["device_count"] == 2 * splits
    spice = (tmp_path / f"{name}.spice").read_text()
    rows = re.findall(r"^M\$\d+ (\\\$\d+) (\\\$\d+) (\\\$\d+) ", spice, re.M)
    assert len(rows) == 2 * splits
    legs = {tuple(sorted(terminals)) for terminals in rows}
    assert len(legs) == 2, f"expected exactly two strapped legs, got {legs}"
    nets = [net for leg in legs for net in leg]
    assert len(set(nets)) == 6, f"the six strapped nets are not distinct: {nets}"


# --- rejection paths --------------------------------------------------------


def test_strap_without_gate_contact_is_rejected(tmp_path, pdk_root):
    """`strap_splits` without `gate_contact` is a `GenError` naming
    `gate_contact` -- a bare-poly gate has no metal pad for the strap's
    mcon, and a silently unstrapped gate is exactly the failure mode this
    parameter exists to remove."""
    with pytest.raises(GenError, match="gate_contact"):
        generate(
            _strap_request(
                tmp_path,
                pdk_root,
                {"strap_splits": True, "gate_contact": False},
                "dp_nogate",
            )
        )


def test_strap_below_min_width_is_rejected(tmp_path, pdk_root):
    """`strap_splits` on a device narrower than the four S/D tracks need is
    a `GenError` stating the floor -- never a silently under-strapped cell."""
    with pytest.raises(GenError, match=r"w_um"):
        generate(
            _strap_request(
                tmp_path, pdk_root, {"strap_splits": True, "w_um": 1.0}, "dp_narrow"
            )
        )


def test_strap_on_unverified_family_is_rejected(tmp_path):
    """`strap_splits` is sky130-verified only: another family is an
    application error naming the family, raised before any geometry is
    drawn (issue #5557's "never unverified geometry" bar)."""
    root = tmp_path / "pdk_install"
    _make_install(root, "gf180mcuD")
    with pytest.raises(GenError, match="gf180mcu"):
        generate(
            {
                "generator": "diff_pair",
                "pdk": {"variant": "gf180mcuD", "root": str(root)},
                "params": {
                    "w_um": 3.0,
                    "l_um": 0.5,
                    "gate_contact": True,
                    "strap_splits": True,
                },
                "options": {"output": str(tmp_path / "dp_gf180.gds")},
            }
        )
