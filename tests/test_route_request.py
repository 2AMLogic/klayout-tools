"""``klt route`` request parsing rejects non-finite / oversized numbers (#6227)."""

import pytest

from klayout_tools.route_request import RouteError, parse_request

LAYERS = [{"layer": 68, "datatype": 5}]
WIDTH_MSG = r"request\.routing\.width_um is required \(a number > 0\)"
DIR_MSG = r"direction_deg must be one of 0, 90, 180, 270"


def _request(*, width_um=0.14, direction_deg=90):
    return {
        "layout": {"gds_path": "top.gds", "top_cell": "chip_top"},
        "pdk": {"variant": "sky130A"},
        "routing": {"layer_role": "metal2", "width_um": width_um},
        "nets": [
            {
                "net": "LINK",
                "pins": [
                    {
                        "cell": "cell_a",
                        "label": "OUT",
                        "layers": LAYERS,
                        "direction_deg": direction_deg,
                    },
                    {"cell": "cell_b", "label": "IN", "layers": LAYERS},
                ],
            }
        ],
    }


def test_valid_values_unchanged(tmp_path):
    parsed = parse_request(_request(), str(tmp_path))
    assert parsed["width_um"] == 0.14


@pytest.mark.parametrize(
    "bad", [10**400, -(10**400), float("inf"), float("-inf"), float("nan")]
)
def test_non_finite_width_is_a_route_error(tmp_path, bad):
    with pytest.raises(RouteError, match=WIDTH_MSG):
        parse_request(_request(width_um=bad), str(tmp_path))


@pytest.mark.parametrize("bad", [10**400, -(10**400), float("inf"), float("nan")])
def test_non_finite_direction_is_a_route_error(tmp_path, bad):
    with pytest.raises(RouteError, match=DIR_MSG):
        parse_request(_request(direction_deg=bad), str(tmp_path))
