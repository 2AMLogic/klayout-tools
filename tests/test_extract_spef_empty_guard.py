"""``_write_spef`` refuses a SPEF with no ``*D_NET`` block (issue #4705)."""

from __future__ import annotations

import pytest

from klayout_tools.extract_spef import ExtractError, _write_spef


def _write(tmp_path, nets):
    path = tmp_path / "out.spef"
    skipped = _write_spef(
        str(path),
        design_name="top",
        klt_version=None,
        parasitics_report={"nets": nets},
        port_names=[],
    )
    return path, skipped


def test_empty_nets_raises_without_writing(tmp_path):
    with pytest.raises(ExtractError, match=r"no \*D_NET blocks.*0 net\(s\)"):
        _write(tmp_path, [])
    assert not (tmp_path / "out.spef").exists()


def test_all_nets_skipped_raises_naming_them(tmp_path):
    # A DEF connection map that does not know the extracted net makes
    # `_drop_floating_duplicate_nets` omit it, leaving nothing to write.
    entry = {"net": "$I2", "hub_net": "$I2", "terminals": [], "coupled": []}
    with pytest.raises(ExtractError, match=r"1 net\(s\) extracted, 1 omitted: \$I2"):
        _write_spef(
            str(tmp_path / "out.spef"),
            design_name="top",
            klt_version=None,
            parasitics_report={"nets": [entry]},
            port_names=[],
            net_instance_pins={"other": [("u1", "A")]},
        )
    assert not (tmp_path / "out.spef").exists()
