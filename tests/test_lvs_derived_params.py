"""``as/ad/ps/pd`` fold under a ``w_um`` finding (issue #4729)."""

from klayout_tools.lvs_mismatch import _fold_derived_param_entries, _mismatch


def _entry(name):
    return _mismatch(
        "device.property",
        "error",
        f"matched device parameter '{name}' differs",
        "both",
        property_={"name": name, "layout": 1.0, "reference": 2.0},
    )


def test_derived_params_fold_under_w():
    out = _fold_derived_param_entries(
        [_entry(n) for n in ("w_um", "as", "ad", "ps", "pd")]
    )
    assert len(out) == 1
    assert out[0]["property"]["name"] == "w_um"
    assert [d["name"] for d in out[0]["details"]["derived"]] == ["as", "ad", "ps", "pd"]


def test_derived_params_stay_without_w():
    entries = [_entry("l_um"), _entry("as")]
    assert _fold_derived_param_entries(entries) == entries
