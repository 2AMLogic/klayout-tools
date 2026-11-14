"""Tests for ``options.boundary`` on ``klt gen-compose`` (#3998)."""

import pytest

from klayout_tools import gen, pdk
from klayout_tools.gen_compose import GenComposeError, compose


def _make_install(root, variant):
    (root / variant / "libs.tech").mkdir(parents=True)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _gen_block_variant(tmp_path, pdk_root, variant, generator, cell_name, **params):
    request = {
        "schema": gen.REQUEST_SCHEMA,
        "generator": generator,
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "params": params,
        "options": {
            "cell_name": cell_name,
            "output": str(tmp_path / f"{cell_name}.gds"),
        },
    }
    return gen.generate(request)


def _gen_block(tmp_path, pdk_root, generator, cell_name, **params):
    return _gen_block_variant(
        tmp_path, pdk_root, "sky130A", generator, cell_name, **params
    )


def _boundary_compose(tmp_path, pdk_root, variant, boundary, name="bnd"):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    options = {"output": str(tmp_path / f"{name}.gds"), "cell_name": name}
    if boundary != "absent":
        options["boundary"] = boundary
    return compose(
        {
            "pdk": {"variant": variant, "root": str(pdk_root)},
            "blocks": [{"id": "b1", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["b1"]},
            "options": options,
        }
    )


def _boundary_shapes(path, cell_name):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(path)
    idx = layout.find_layer(235, 4)
    if idx is None:
        return [], layout.dbu
    return [s.bbox() for s in layout.cell(cell_name).shapes(idx).each()], layout.dbu


def test_boundary_draws_one_235_4_rect_with_requested_extent(tmp_path, pdk_root):
    bbox = {"x0": -5, "y0": 2, "x1": 2000, "y1": 1500.5}
    res = _boundary_compose(tmp_path, pdk_root, "sky130A", {"bbox_um": bbox})
    boxes, dbu = _boundary_shapes(res["gds_path"], "bnd")
    assert len(boxes) == 1
    b = boxes[0]
    assert (b.left * dbu, b.bottom * dbu, b.right * dbu, b.top * dbu) == (
        pytest.approx(-5),
        pytest.approx(2),
        pytest.approx(2000),
        pytest.approx(1500.5),
    )


def test_boundary_absent_or_null_draws_nothing(tmp_path, pdk_root):
    for i, value in enumerate(["absent", None]):
        res = _boundary_compose(tmp_path, pdk_root, "sky130A", value, name=f"n{i}")
        assert _boundary_shapes(res["gds_path"], f"n{i}")[0] == []


@pytest.mark.parametrize(
    "boundary",
    [
        "nope",
        {},
        {"bbox_um": {"x0": 0, "y0": 0, "x1": 10}},
        {"bbox_um": {"x0": 0, "y0": 0, "x1": "a", "y1": 10}},
        {"bbox_um": {"x0": 0, "y0": 0, "x1": 0, "y1": 10}},
        {"bbox_um": {"x0": 0, "y0": 5, "x1": 10, "y1": -5}},
    ],
)
def test_boundary_rejects_invalid(tmp_path, pdk_root, boundary):
    with pytest.raises(GenComposeError, match="options.boundary"):
        _boundary_compose(tmp_path, pdk_root, "sky130A", boundary)


def test_boundary_gf180mcu_draws_235_4(tmp_path, both_pdk_root):
    r1 = _gen_block_variant(
        tmp_path, both_pdk_root, "gf180mcuD", "resistor_strip", "r1"
    )
    res = compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "blocks": [{"id": "b1", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["b1"]},
            "options": {
                "output": str(tmp_path / "g.gds"),
                "cell_name": "g",
                "boundary": {"bbox_um": {"x0": 0, "y0": 0, "x1": 300, "y1": 200}},
            },
        }
    )
    boxes, dbu = _boundary_shapes(res["gds_path"], "g")
    assert len(boxes) == 1
    assert (boxes[0].right * dbu, boxes[0].top * dbu) == (
        pytest.approx(300),
        pytest.approx(200),
    )
