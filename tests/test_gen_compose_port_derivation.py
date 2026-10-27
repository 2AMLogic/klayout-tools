"""``blocks[].cell.ports_from_labels`` -- label-derived ports and the via-stack
landing search (#2927). Synthetic sky130 macros only (open PDK layer numbers)."""

import klayout.db as kdb
import pytest

from klayout_tools.gen_compose import GenComposeError, _parse_blocks, compose
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

MET1 = (68, 20)
MET1_PIN = (68, 5)
MET2 = (69, 20)
MET2_PIN = (69, 5)
VIA1 = (68, 44)
KEEPOUT = (200, 0)


def _make_macro(tmp_path, shapes, labels):
    """``shapes``: ``[(layer_pair, (x0, y0, x1, y1) um)]``; ``labels``:
    ``[(layer_pair, text, x, y)]``. Returns the GDS path (cell ``macro``)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("macro")
    for (layer, datatype), (x0, y0, x1, y1) in shapes:
        index = layout.layer(layer, datatype)
        top.shapes(index).insert(kdb.DBox(x0, y0, x1, y1))
    for (layer, datatype), text, x, y in labels:
        index = layout.layer(layer, datatype)
        top.shapes(index).insert(kdb.DText(text, x, y))
    path = tmp_path / "macro.gds"
    layout.write(str(path))
    return str(path)


def _blocks(gds, spec, ports=None):
    cell = {
        "gds_path": gds,
        "cell_name": "macro",
        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 12.0, "y1": 4.0},
        "ports_from_labels": spec,
    }
    if ports is not None:
        cell["ports"] = ports
    return _parse_blocks([{"id": "m", "cell": cell}], None, "sky130A")


def _spec(names, access=None, layers=((68, 5),)):
    spec = {
        "layers": [{"layer": layer, "datatype": dt} for layer, dt in layers],
        "names": names,
    }
    if access is not None:
        spec["access"] = access
    return spec


ACCESS = {"up_to": "metal3"}  # met2: one via hop from met1


def _two_strip_macro(tmp_path, foreign_bottom=0.55):
    return _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.5)),  # net A
            (MET1, (6.0, foreign_bottom, 10.0, 2.0)),  # foreign net B (above)
            (MET1, (6.0, -1.5, 10.0, -0.05)),  # foreign net C (below)
        ],
        [(MET1_PIN, "A", 8.0, 0.25), (MET1_PIN, "B", 8.0, 1.0)],
    )


def test_label_without_access_derives_port_at_label(tmp_path):
    gds = _two_strip_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"]))
    port = blocks["m"]["ports"]["A"]
    assert (port["x_um"], port["y_um"]) == pytest.approx((8.0, 0.25))
    assert port["layer"] == {"layer": 68, "datatype": 20}
    assert port["net"] == "A"
    assert blocks["m"]["derived_ports"][0]["landing"] is None


def test_landing_avoids_foreign_net_clearance(tmp_path):
    gds = _two_strip_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], ACCESS))
    landing = blocks["m"]["derived_ports"][0]["landing"]
    # Under B's shadow (x >= ~5.6) the met1 pad would sit < 0.14um from B.
    assert landing["x_um"] < 6.0
    assert landing["layer"] == {"layer": 68, "datatype": 20}
    assert landing["via_count"] == 1
    assert landing["up_to"] == "metal3"
    port = blocks["m"]["ports"]["A"]
    assert (port["x_um"], port["y_um"]) == (landing["x_um"], landing["y_um"])


def test_landing_is_deterministic(tmp_path):
    gds = _two_strip_macro(tmp_path)
    first = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"]
    second = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"]
    assert first == second


def test_no_legal_point_lists_rejection_reasons(tmp_path):
    # Foreign strip hugs the whole net: every candidate breaks clearance.
    gds = _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.5)),
            (MET1, (0.0, 0.55, 10.0, 2.0)),
            (MET1, (0.0, -1.5, 10.0, -0.05)),
        ],
        [(MET1_PIN, "A", 5.0, 0.25)],
    )
    with pytest.raises(GenComposeError, match=r"no legal landing.*clearance: [1-9]"):
        _blocks(gds, _spec(["A"], ACCESS))


def _thin_wire_macro(tmp_path):
    # 0.4 um x 1400 um wire: bbox grid at the default step is >4M points.
    return _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 0.4, 1400.0))],
        [(MET1_PIN, "A", 0.2, 700.0)],
    )


def test_long_thin_wire_default_grid_derives_port(tmp_path):
    gds = _thin_wire_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], ACCESS))
    assert "A" in blocks["m"]["ports"]


def test_long_thin_wire_coarse_grid_derives_port(tmp_path):
    gds = _thin_wire_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], {**ACCESS, "grid_um": 10.0}))
    port = blocks["m"]["ports"]["A"]
    assert 0.0 <= port["x_um"] <= 0.4


def test_no_candidate_error_names_cause(tmp_path, monkeypatch):
    import klayout_tools.gen_compose_port_access as access_mod

    # Force the enumerator to yield nothing so _search_landing reaches the
    # empty-candidate error instead of the per-category rejection summary.
    monkeypatch.setattr(access_mod, "_grid_candidates", lambda *a, **k: ([], False))
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(
        GenComposeError,
        match=r"no candidate landing point for port 'A'.*lower access\.grid_um",
    ):
        _blocks(gds, _spec(["A"], ACCESS))


def test_keepout_layer_rejects_and_steers(tmp_path):
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 1.0)), (KEEPOUT, (0.0, -1.0, 5.0, 2.0))],
        [(MET1_PIN, "A", 2.0, 0.5)],
    )
    access = {**ACCESS, "keepout_layers": [{"layer": 200, "datatype": 0}]}
    landing = _blocks(gds, _spec(["A"], access))["m"]["derived_ports"][0]["landing"]
    assert landing["x_um"] > 5.0

    full = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 1.0)), (KEEPOUT, (-1.0, -1.0, 11.0, 2.0))],
        [(MET1_PIN, "A", 2.0, 0.5)],
    )
    with pytest.raises(GenComposeError, match=r"keep_out: [1-9]"):
        _blocks(full, _spec(["A"], access))


def test_via_spacing_against_existing_via(tmp_path):
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 1.0)), (VIA1, (2.925, 0.425, 3.075, 0.575))],
        [(MET1_PIN, "A", 3.0, 0.5)],
    )
    landing = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    dist = ((landing["x_um"] - 3.0) ** 2 + (landing["y_um"] - 0.5) ** 2) ** 0.5
    assert dist > 0.15


def test_unmatched_name_is_hard_error(tmp_path):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match="'ZZ' matches no text label"):
        _blocks(gds, _spec(["ZZ"]))


def test_label_on_layer_not_listed_is_unmatched(tmp_path):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match="matches no text label"):
        _blocks(gds, _spec(["A"], layers=((69, 5),)))


def test_label_on_no_conductor_is_hard_error(tmp_path):
    gds = _make_macro(
        tmp_path, [(MET1, (0.0, 0.0, 1.0, 1.0))], [(MET1_PIN, "FLOAT", 9.0, 3.0)]
    )
    with pytest.raises(GenComposeError, match="'FLOAT' sits on no conductor"):
        _blocks(gds, _spec(["FLOAT"]))


def test_declared_and_derived_name_is_ambiguous(tmp_path):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match="ambiguous"):
        _blocks(gds, _spec(["A"]), ports=[{"name": "A"}])


def test_declared_and_derived_ports_mix(tmp_path):
    gds = _two_strip_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"]), ports=[{"name": "VDD"}])
    assert set(blocks["m"]["ports"]) == {"VDD", "A"}


def test_option_omitted_is_unchanged(tmp_path):
    gds = _two_strip_macro(tmp_path)
    blocks = _parse_blocks(
        [
            {
                "id": "m",
                "cell": {
                    "gds_path": gds,
                    "cell_name": "macro",
                    "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 12.0, "y1": 4.0},
                    "ports": [{"name": "A"}],
                },
            }
        ]
    )
    assert blocks["m"]["derived_ports"] == []
    assert set(blocks["m"]["ports"]) == {"A"}


def test_label_on_multilayer_net_reports_label_metal(tmp_path):
    gds = _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 4.0, 1.0)),
            (VIA1, (0.425, 0.425, 0.575, 0.575)),
            (MET2, (0.0, 0.0, 1.0, 1.0)),
        ],
        [(MET2_PIN, "A", 0.5, 0.5)],
    )
    port = _blocks(gds, _spec(["A"], layers=((69, 5),)))["m"]["ports"]["A"]
    assert port["layer"] == {"layer": 69, "datatype": 20}


def test_two_labels_on_one_net_give_two_ports(tmp_path):
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 1.0))],
        [(MET1_PIN, "A1", 1.0, 0.5), (MET1_PIN, "A2", 9.0, 0.5)],
    )
    ports = _blocks(gds, _spec(["A1", "A2"], ACCESS))["m"]["ports"]
    assert ports["A1"]["x_um"] < ports["A2"]["x_um"]


def test_per_name_access_overrides_shared(tmp_path):
    gds = _two_strip_macro(tmp_path)
    spec = _spec([{"name": "A", "access": ACCESS}, "B"])
    reports = _blocks(gds, spec)["m"]["derived_ports"]
    assert reports[0]["landing"] is not None
    assert reports[1]["landing"] is None


@pytest.mark.parametrize(
    "access,match",
    [
        ({}, "up_to is required"),
        ({"up_to": "metal3", "via_array": [17, 1]}, "sides must be <= 16"),
        ({"up_to": "metal3", "via_array": [2]}, "integers >= 1"),
        ({"up_to": "metal3", "via_array": [0, 1]}, "integers >= 1"),
        ({"up_to": "metal3", "bogus": 1}, "unknown field"),
        ({"up_to": "metal3", "keepout_layers": [{"layer": 1}]}, "datatype"),
    ],
)
def test_access_validation(tmp_path, access, match):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match=match):
        _blocks(gds, _spec(["A"], access))


def test_compose_reports_derived_ports(tmp_path, pdk_root):
    gds = _two_strip_macro(tmp_path)
    response = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {
                    "id": "m",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "macro",
                        "ports_from_labels": _spec(["A"], ACCESS),
                    },
                }
            ],
            "placement": {"strategy": "row", "order": ["m"], "spacing_um": 1.0},
            "options": {"output": str(tmp_path / "top.gds")},
        }
    )
    derived = response["blocks"][0]["derived_ports"]
    assert derived[0]["name"] == "A"
    assert derived[0]["landing"]["via_count"] == 1


def _array_macro(tmp_path, name="macro.gds"):
    """Strip A with foreign nets hugging both sides of its right half."""
    return _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.6)),
            (MET1, (4.0, 0.9, 10.0, 2.0)),
            (MET1, (4.0, -2.0, 10.0, -0.3)),
        ],
        [(MET1_PIN, "A", 8.0, 0.3), (MET1_PIN, "B", 8.0, 1.0)],
    )


def test_via_array_reported_and_widens_footprint(tmp_path):
    gds = _array_macro(tmp_path)
    single = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    arr_access = {**ACCESS, "via_array": [3, 3]}
    blocks = _blocks(gds, _spec(["A"], arr_access))
    landing = blocks["m"]["derived_ports"][0]["landing"]
    assert single["via_count"] == 1
    assert landing["via_array"] == [3, 3]
    assert landing["via_count"] == 9
    # The 3x3 pad is wider than the gap under B, so the landing must move
    # left of B's shadow; the single cut fits right at the label.
    assert single["x_um"] > 7.0
    assert landing["x_um"] < 4.5
    port = blocks["m"]["ports"]["A"]
    assert port["via_array"]["rows"] == port["via_array"]["cols"] == 3
    assert port["via_array"]["pitch_um"]["68/44"] > 0.15


def test_via_array_one_by_one_adds_no_port_array(tmp_path):
    gds = _array_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], {**ACCESS, "via_array": [1, 1]}))
    assert "via_array" not in blocks["m"]["ports"]["A"]


def test_via_array_cuts_are_drawn_on_the_landing(tmp_path, pdk_root):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    gds_a = _array_macro(a_dir)
    gds_b = _make_macro(
        b_dir,
        [(MET1, (0.0, 0.0, 10.0, 0.6))],
        [],
    )

    def run(array, output):
        access = {**ACCESS, "via_array": array}
        return compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {
                        "id": "m",
                        "cell": {
                            "gds_path": gds_a,
                            "cell_name": "macro",
                            "ports_from_labels": _spec(["A"], access),
                        },
                    },
                    {
                        "id": "n",
                        "cell": {
                            "gds_path": gds_b,
                            "cell_name": "macro",
                            "bbox_um": {"x0": 0.0, "y0": -0.5, "x1": 10.0, "y1": 1.0},
                            "ports": [
                                {
                                    "name": "C",
                                    "x_um": 0.3,
                                    "y_um": 0.3,
                                    "layer": {"layer": 68, "datatype": 20},
                                    "direction_deg": 180,
                                }
                            ],
                        },
                    },
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["m", "n"],
                    "spacing_um": 3.0,
                },
                "connectivity": [
                    {
                        "net": "N",
                        "pins": [
                            {"block": "m", "port": "A"},
                            {"block": "n", "port": "C"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal3", "width_um": 0.28},
                "options": {"output": str(output)},
            }
        )

    def cuts(output):
        layout = kdb.Layout()
        layout.read(str(output))
        region = kdb.Region(layout.top_cell().begin_shapes_rec(layout.layer(*VIA1)))
        return [polygon.bbox() for polygon in region.each()]

    out1 = tmp_path / "one.gds"
    out2 = tmp_path / "two.gds"
    assert run([1, 1], out1)["nets"][0]["routed"] is True
    assert run([2, 2], out2)["nets"][0]["routed"] is True
    one = cuts(out1)
    two = cuts(out2)
    assert len(two) == len(one) + 3
    # Same-size cuts, none overlapping, none closer than the deck spacing.
    assert len({(c.width(), c.height()) for c in two}) == 1
    # Merged-region count equals the cut count: no two cuts touch.
    assert kdb.Region([kdb.Polygon(c) for c in two]).merged().count() == len(two)
