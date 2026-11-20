"""Cut spacing at the outer edge of a sky130A label-derived via array."""

import pytest

from klayout_tools.gen_compose import GenComposeError
from test_gen_compose import _isolate as _isolate
from test_gen_compose_port_derivation import _blocks, _make_macro, _spec


@pytest.mark.parametrize(("layer", "up_to"), [(69, "metal4"), (70, "metal5")])
@pytest.mark.parametrize("gap_dbu", [200, 199])
def test_via_array_cut_spacing(tmp_path, layer, up_to, gap_dbu):
    # The sole candidate is at (1, 1) um. Its 2x2 array ends at x = 1300 DBU.
    cut_left = (1300 + gap_dbu) * 0.001
    pin = (layer, 5)
    gds = _make_macro(
        tmp_path,
        [
            ((layer, 20), (0.98, 0.98, 1.02, 1.02)),
            ((layer, 44), (cut_left, 0.8, cut_left + 0.2, 1.0)),
        ],
        [(pin, "A", 1.0, 1.0)],
    )
    access = {"up_to": up_to, "via_array": [2, 2], "grid_um": 1.0}
    spec = _spec(["A"], access, layers=[pin])
    if gap_dbu == 199:
        with pytest.raises(GenComposeError, match=r"via_spacing: 1\b"):
            _blocks(gds, spec)
    else:
        landing = _blocks(gds, spec)["m"]["derived_ports"][0]["landing"]
        assert (landing["x_um"], landing["y_um"]) == (1.0, 1.0)
