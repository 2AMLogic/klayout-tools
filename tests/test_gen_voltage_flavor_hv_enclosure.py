"""Regression tests for issue #7061: a sky130 ``hvi``-marked ``mos_array`` /
``diff_pair`` must satisfy the native ``sky130A.lydrc`` FEOL enclosures of
that marker and of the well it intersects, measured on the emitted GDS.

The checks below transcribe the native deck's own statements (open_pdks
``libs.tech/klayout/drc/sky130A.lydrc``) onto KLayout's ``Region`` DRC
primitives, euclidian metrics, the same layers:

- ``hvdifftap.22`` -- ``hvi.enclosing(difftap, 0.18)``
- ``hvdifftap.17`` -- ``hvi.and(nwell).enclosing(diff, 0.33)``
- ``hvdifftap.19`` -- ``hvi.and(nwell).enclosing(tap, 0.33)``
- ``hvdifftap.20`` -- ``hvi.and(nwell).separation(tap, 0.43)``
- ``licon.15`` -- ``npc.enclosing(licon.and(poly), 0.1)``

The companion native ``sky130A_mr.drc`` checks ``nwell.9`` on nwell
polygons that interact with HVI.

Before the fix the marker sat 0.15um past the core (``.22``), a ringless
pfet's nwell and marker both stopped at 0.15um (``.17``), and a ringed
pfet's well/marker stopped 0.2um past the n-tap ring (``.19``).
"""

import klayout.db as kdb
import pytest

from klayout_tools.gen import generate

_DIFF = (65, 20)
_TAP = (65, 44)
_NWELL = (64, 20)
_HVI = (75, 20)
_LVTN = (125, 44)
_POLY = (66, 20)
_LICON = (66, 44)
_NPC = (95, 20)


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _gen(tmp_path, pdk_root, generator, params, name):
    output = tmp_path / f"{name}.gds"
    generate(
        {
            "generator": generator,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": params,
            "options": {"output": str(output), "cell_name": "top"},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    return layout


def _region(layout, layer_datatype):
    index = layout.find_layer(*layer_datatype)
    if index is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(index)).merged()


def _params(generator, flavor, ring, voltage_flavor="hvi"):
    params = {
        "w_um": 2,
        "l_um": 10,
        "flavor": flavor,
        "voltage_flavor": voltage_flavor,
        "gate_contact": True,
        "add_guard_ring": ring,
    }
    if generator == "mos_array":
        params.update(rows=1, cols=1, dummy=0)
    return params


def _native_hv_counts(layout, override=None):
    """Per-rule violation counts of the native deck's HV statements;
    ``override`` substitutes a layer's region (the positive control)."""
    um = int(round(1 / layout.dbu))
    override = override or {}

    def layer(key):
        return override[key] if key in override else _region(layout, key)

    diff = _region(layout, _DIFF)
    tap = _region(layout, _TAP)
    hvi = layer(_HVI)
    nwell = layer(_NWELL)
    hvnwell = hvi & nwell
    licon_poly = _region(layout, _LICON) & _region(layout, _POLY)
    metrics = kdb.Metrics.Euclidian
    return {
        "nwell.9": nwell.interacting(hvnwell) - hvi,
        "hvdifftap.17": hvnwell.enclosing_check(diff, 33 * um // 100, False, metrics),
        "hvdifftap.19": hvnwell.enclosing_check(tap, 33 * um // 100, False, metrics),
        "hvdifftap.20": hvnwell.separation_check(tap, 43 * um // 100, False, metrics),
        "hvdifftap.22": hvi.enclosing_check(diff + tap, 18 * um // 100, False, metrics),
        "licon.15": _region(layout, _NPC).enclosing_check(
            licon_poly, 10 * um // 100, False, metrics
        ),
    }


_CASES = [
    (generator, flavor, ring)
    for generator in ("mos_array", "diff_pair")
    for flavor in ("nfet", "pfet")
    for ring in (False, True)
]


@pytest.mark.parametrize(
    ("generator", "flavor", "ring"),
    _CASES,
    ids=[f"{g}-{f}-{'ring' if r else 'noring'}" for g, f, r in _CASES],
)
def test_hvi_marker_and_well_meet_native_enclosures(
    tmp_path, pdk_root, generator, flavor, ring
):
    layout = _gen(
        tmp_path, pdk_root, generator, _params(generator, flavor, ring), "case"
    )
    assert not _region(layout, _HVI).is_empty()
    if flavor == "pfet":
        assert not (_region(layout, _HVI) & _region(layout, _NWELL)).is_empty()
    counts = {rule: edges.count() for rule, edges in _native_hv_counts(layout).items()}
    assert counts == dict.fromkeys(counts, 0)


def test_hv_checks_catch_a_marker_drawn_at_the_old_margin(tmp_path, pdk_root):
    """Positive control for the checks themselves: shrinking the drawn
    ``hvi``/nwell back to the pre-fix 0.15um core margin trips ``.22`` and
    ``.17`` on a ringless pfet."""
    layout = _gen(
        tmp_path, pdk_root, "mos_array", _params("mos_array", "pfet", False), "ctl"
    )
    shrink = int(round(0.18 / layout.dbu))  # 0.33um -> 0.15um past the core
    old = {key: _region(layout, key).sized(-shrink) for key in (_HVI, _NWELL)}
    counts = {
        rule: edges.count() for rule, edges in _native_hv_counts(layout, old).items()
    }
    assert counts["hvdifftap.22"] > 0
    assert counts["hvdifftap.17"] > 0


@pytest.mark.parametrize("generator", ["mos_array", "diff_pair"])
def test_non_hvi_sky130_marker_keeps_its_geometry(tmp_path, pdk_root, generator):
    """``lvt`` draws ``lvtn`` alone, which the HV enclosures do not govern:
    its pfet well is the same shape as an unflavoured pfet's, and its marker
    is not grown to the HV margin."""
    lvt = _gen(
        tmp_path,
        pdk_root,
        generator,
        _params(generator, "pfet", False, voltage_flavor="lvt"),
        "lvt",
    )
    plain = _gen(
        tmp_path,
        pdk_root,
        generator,
        _params(generator, "pfet", False, voltage_flavor=""),
        "plain",
    )
    hvi = _gen(tmp_path, pdk_root, generator, _params(generator, "pfet", False), "hv")
    assert (_region(lvt, _NWELL) ^ _region(plain, _NWELL)).is_empty()
    assert _region(lvt, _HVI).is_empty()
    assert _region(lvt, _LVTN).bbox() == _region(plain, _NWELL).bbox()
    assert _region(hvi, _NWELL).bbox().width() > _region(plain, _NWELL).bbox().width()
