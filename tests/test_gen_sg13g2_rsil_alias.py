"""Issue #5288: sg13g2 `rsil` is an alias of `generic` (same masks);
sky130/gf180mcu still reject it."""

from klayout_tools.gen_layer_params import _PDK_RES_FLAVOR_LAYERS as TABLE


def test_sg13g2_rsil_alias_matches_generic_masks():
    assert TABLE["sg13g2"]["rsil"] == TABLE["sg13g2"]["generic"]
    assert "rsil" not in TABLE["sky130"]
    assert "rsil" not in TABLE["gf180mcu"]
