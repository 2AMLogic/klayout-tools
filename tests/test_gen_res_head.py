"""``res_array`` end-head length regressions (issue #5704).

sky130 ``high``/``xhigh`` precision poly resistors draw their contacted end
heads 2.16um long -- the PDK's ``licon.1c + li.5`` contact-extension rule --
resolved per (PDK family, flavor) by :mod:`klayout_tools.gen_res_head`. Split
from ``test_gen.py`` (file-size ratchet); the sibling precision-resistor tests
(``test_sky130_precision_res_*``) stay there.
"""

import pytest

from klayout_tools import gen, pdk
from klayout_tools.gen import generate

# sky130 li1 (local interconnect) drawing layer; gf180mcu Contact layer.
_SKY130_LI1_LAYER = (67, 20)
_GF180_CONTACT_LAYER = (33, 0)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_gen.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _install(tmp_path, *variants):
    """Fabricate open_pdks variant directories for the PDK resolver."""
    root = tmp_path / "pdk_install"
    for variant in variants:
        (root / variant / "libs.tech").mkdir(parents=True)
    return root


@pytest.fixture()
def pdk_root(tmp_path):
    return _install(tmp_path, "sky130A")


@pytest.fixture()
def both_pdk_root(tmp_path):
    return _install(tmp_path, "sky130A", "gf180mcuD")


def _drawn_boxes_dbu(path, layer_pair):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(path))
    top = layout.top_cell()
    top.flatten(True)
    return [
        s.box for s in top.shapes(layout.layer(*layer_pair)).each() if s.is_box()
    ], layout.dbu


def _gen_sky130_res(tmp_path, pdk_root, flavor, extra=None):
    output = tmp_path / f"res_{flavor}.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": flavor, "num": 2, "dummy": 0, **(extra or {})},
            "options": {"output": str(output)},
        }
    )
    return output, report


@pytest.mark.parametrize("flavor", ["high", "xhigh"])
def test_sky130_precision_res_head_is_2160nm_long(tmp_path, pdk_root, flavor):
    """Issue #5704: the end head extends the body by the PDK's 2.16um
    precision-resistor contact-extension rule (``licon.1c + li.5`` -- the
    foundry's ``res_to_endcont 1.985 + end_contact_size/2 + poly_surround``),
    by default, resolved from a PDK table with no new request parameter.
    Only the heads and the unit pitch move: the recognised body segment (and
    so the extracted ``L``/``R``), the pad height and the centred
    0.19um x 2.0um slot are unchanged."""
    output, report = _gen_sky130_res(
        tmp_path, pdk_root, flavor, {"length_um": 9.765, "width_um": 1.41, "num": 1}
    )
    pads, dbu = _drawn_boxes_dbu(output, _SKY130_LI1_LAYER)
    assert dbu == 0.001
    # One head pad per end: each 2.16um long along x.
    assert len(pads) == 2
    assert {p.width() for p in pads} == {2160}
    # The body marker (poly.res 66/13) still spans exactly the requested
    # length -- the extracted L/R cannot have moved.
    body, _ = _drawn_boxes_dbu(output, (66, 13))
    assert {b.width() for b in body} == {9765}
    # Ports sit at the drawn head centres: head_len/2 in from each edge.
    xs = sorted(p["x_um"] for p in report["ports"])
    assert xs[0] == pytest.approx(2.16 / 2)
    assert xs[-1] == pytest.approx(2.0 * 2.16 + 9.765 - 2.16 / 2)
    # The describe report discloses the head length.
    assert any("extended to 2.16um" in n for n in report["drc_hints"]["notes"])


def test_res_unit_head_len_extends_pitch_without_touching_body():
    """Issue #5704: ``head_len_um`` is a pure ``max()`` floor on the end-head
    region -- the body segment, pad height and centred contact are identical
    to the ``0.0`` (generic) call."""
    base = gen._res_unit_layout(2.0, 1.41, 0.0, 0.0, 0.19, 2.0)
    headed = gen._res_unit_layout(2.0, 1.41, 0.0, 0.0, 0.19, 2.0, head_len_um=2.16)
    assert base["total_len_um"] == pytest.approx(2.0 + 2 * 0.39)
    assert headed["total_len_um"] == pytest.approx(2.0 + 2 * 2.16)
    # The body segment's size is unchanged; only its offset (behind the
    # longer head) and the unit pitch move.
    assert [(b[2] - b[0], b[3] - b[1]) for b in headed["boxes_um"]["marker"]] == [
        (b[2] - b[0], b[3] - b[1]) for b in base["boxes_um"]["marker"]
    ]
    assert headed["pad_height_um"] == base["pad_height_um"]


def test_res_head_len_resolved_from_pdk_table():
    """Issue #5704: the 2.16um head resolves from a per-(family, flavor)
    table -- sky130 high/xhigh only; generic, other families and
    ``metal_level`` requests keep the 0.0 (generic enclosure-derived) head."""
    from klayout_tools.gen_res_head import _res_head_len_um

    assert _res_head_len_um("sky130", "high") == 2.16
    assert _res_head_len_um("sky130", "xhigh") == 2.16
    assert _res_head_len_um("sky130", "generic") == 0.0
    assert _res_head_len_um("gf180mcu", "1k") == 0.0
    assert _res_head_len_um("sg13g2", "rhigh") == 0.0
    assert _res_head_len_um("sky130", "high", metal_level=1) == 0.0


def test_res_head_len_leaves_generic_and_gf180mcu_geometry_unchanged(
    tmp_path, both_pdk_root
):
    """Issue #5704 control: families/flavours outside the head-length table
    draw exactly the pre-#5704 geometry -- a generic sky130 head stays
    0.42um (0.22 + 2 x 0.1) and a gf180mcu ``1k`` head stays 0.52um (0.22 +
    2 x 0.15 SAB clearance), ports at the same head centres as before."""
    output = tmp_path / "generic.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "params": {
                "length_um": 2.0,
                "width_um": 1.0,
                "num": 1,
                "dummy": 0,
                "flavor": "generic",
            },
            "options": {"output": str(output)},
        }
    )
    pads, dbu = _drawn_boxes_dbu(output, _SKY130_LI1_LAYER)
    assert {(p.width(), p.height()) for p in pads} == {
        (round(0.42 / dbu), round(1.0 / dbu))
    }
    assert sorted(p["x_um"] for p in report["ports"]) == pytest.approx(
        [0.42 / 2, 2.0 + 2 * 0.42 - 0.42 / 2]
    )
    gf_output = tmp_path / "gf180.gds"
    gf_report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                "length_um": 2.0,
                "width_um": 1.0,
                "num": 1,
                "dummy": 0,
                "flavor": "1k",
            },
            "options": {"output": str(gf_output)},
        }
    )
    contacts, _ = _drawn_boxes_dbu(gf_output, _GF180_CONTACT_LAYER)
    assert {(c.width(), c.height()) for c in contacts} == {(220, 220)}
    assert sorted(p["x_um"] for p in gf_report["ports"]) == pytest.approx(
        [0.52 / 2, 2.0 + 2 * 0.52 - 0.52 / 2]
    )
