"""`klt extract` / `klt lvs` case-fold net-name collisions (issue #4200)."""

from __future__ import annotations

import pytest

from klayout_tools.extract_report import (
    _case_collision_warnings,
    detect_case_colliding_net_names,
)
from klayout_tools.lvs_mismatch import _annotate_case_collision_merges


def test_detects_case_only_difference():
    assert detect_case_colliding_net_names(["pb", "PB", "nz", "x"]) == [["PB", "pb"]]


def test_three_way_collision_is_one_group():
    assert detect_case_colliding_net_names(["nz", "NZ", "Nz"]) == [["NZ", "Nz", "nz"]]


def test_exact_duplicates_and_clean_names_not_flagged():
    assert detect_case_colliding_net_names(["pb", "pb", "a", "b"]) == []


def test_warning_names_labels_and_hierarchical_cells():
    nets = [{"name": "pb"}, {"name": "PB"}, {"name": "a"}]
    (warning,) = _case_collision_warnings(nets)
    assert "'PB'" in warning and "'pb'" in warning
    assert "--hierarchical-cells" in warning
    assert _case_collision_warnings([{"name": "a"}, {"name": "a"}]) == []


class _Net:
    def __init__(self, name):
        self._name = name

    def expanded_name(self):
        return self._name


class _Circuit:
    def __init__(self, *names):
        self._nets = [_Net(n) for n in names]

    def each_net(self):
        return iter(self._nets)


def _merged():
    return {"category": "net.merged", "severity": "error", "description": "d"}


def test_lvs_note_added_to_net_merged_only_on_inline_collision():
    entries = [_merged(), {"category": "device.unmatched", "description": "u"}]
    _annotate_case_collision_merges(entries, _Circuit("pb", "PB"), True)
    assert "differing only by case" in entries[0]["description"]
    assert entries[0]["severity"] == "error"
    assert entries[1]["description"] == "u"


def test_lvs_note_skipped_without_collision_or_inline():
    entries = [_merged()]
    _annotate_case_collision_merges(entries, _Circuit("a", "b"), True)
    _annotate_case_collision_merges(entries, _Circuit("pb", "PB"), False)
    assert entries[0]["description"] == "d"


def test_run_extract_warns_on_case_colliding_labels(tmp_path):
    from klayout_tools.extract import run_extract
    from test_extract import _make_inverter_layout, _write_gds
    from test_extract_split_labels import _strapped, _with_island

    layout = _with_island(
        _with_island(_strapped(_make_inverter_layout()), "pb", 6000), "PB", 7000
    )
    path = _write_gds(layout, tmp_path / "c.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "c.spice"))
    assert any("differ only by case" in w for w in report["warnings"])

    clean = _write_gds(_strapped(_make_inverter_layout()), tmp_path / "ok.gds")
    report = run_extract(clean, "sky130", output=str(tmp_path / "ok.spice"))
    assert not any("differ only by case" in w for w in report["warnings"])


# --- issue #7091: flat --parasitics SPICE refuses case-fold net names -------


def _relabel(layout, mapping):
    """Rename top-level 67/5 (li1 label) texts per ``mapping``, geometry fixed."""
    import klayout.db as kdb

    li = layout.layer(67, 5)
    top = layout.top_cell()
    for shape in list(top.shapes(li).each()):
        if shape.is_text() and shape.text_string in mapping:
            shape.text = kdb.Text(mapping[shape.text_string], shape.text_trans)
    return layout


def _colliding_gds(tmp_path):
    from test_extract import _make_inverter_layout, _write_gds

    layout = _relabel(_make_inverter_layout(), {"A": "N1", "VGND": "n1"})
    return _write_gds(layout, tmp_path / "two.gds")


def _assert_names_both(exc_info):
    message = str(exc_info.value)
    assert "'N1'" in message and "'n1'" in message
    assert "differ only by case" in message


_KNOWN = "* pre-existing netlist, must survive\n"


@pytest.mark.parametrize("scope", [None, ("VPWR",)], ids=["full-rc", "rc-vpwr"])
def test_parasitic_spice_refuses_case_colliding_clusters(tmp_path, scope):
    from klayout_tools.extract import ExtractError, run_extract

    gds = _colliding_gds(tmp_path)
    fresh = tmp_path / "fresh.spice"
    kwargs = {"parasitics": True}
    if scope is not None:
        kwargs["parasitics_nets"] = scope
    with pytest.raises(ExtractError) as exc_info:
        run_extract(gds, "sky130", output=str(fresh), **kwargs)
    _assert_names_both(exc_info)
    assert not fresh.exists()

    existing = tmp_path / "existing.spice"
    existing.write_text(_KNOWN)
    with pytest.raises(ExtractError):
        run_extract(gds, "sky130", output=str(existing), **kwargs)
    assert existing.read_text() == _KNOWN


def test_parasitic_default_output_is_protected(tmp_path):
    from klayout_tools.extract import ExtractError, run_extract

    gds = _colliding_gds(tmp_path)
    default = tmp_path / "two.spice"
    default.write_text(_KNOWN)
    with pytest.raises(ExtractError) as exc_info:
        run_extract(gds, "sky130", parasitics=True)
    _assert_names_both(exc_info)
    assert default.read_text() == _KNOWN


def test_parasitic_no_netlist_census_keeps_warning(tmp_path):
    from klayout_tools.extract import run_extract

    report = run_extract(
        _colliding_gds(tmp_path), "sky130", parasitics=True, no_netlist=True
    )
    assert any("differ only by case" in w for w in report["warnings"])
    assert report["netlist_path"] is None and report["netlist_sha256"] is None
    assert {"N1", "n1"} <= {n["name"] for n in report["nets"]}


def test_device_only_extraction_keeps_warning_and_writes(tmp_path):
    from klayout_tools.extract import run_extract

    out = tmp_path / "dev.spice"
    report = run_extract(_colliding_gds(tmp_path), "sky130", output=str(out))
    assert any("differ only by case" in w for w in report["warnings"])
    assert out.exists()


@pytest.mark.parametrize("extra_a_label", [None, "a"], ids=["clean", "A|a"])
def test_parasitic_spice_still_written_without_cross_net_collision(
    tmp_path, extra_a_label
):
    from klayout_tools.extract import run_extract
    from test_extract import _make_inverter_layout, _write_gds

    layout = _make_inverter_layout(extra_a_label=extra_a_label)
    gds = _write_gds(layout, tmp_path / "ok.gds")
    out = tmp_path / "ok.spice"
    report = run_extract(gds, "sky130", output=str(out), parasitics=True)
    assert out.exists() and report["netlist_path"] == str(out)
    assert not any("differ only by case" in w for w in report["warnings"])
    if extra_a_label:
        assert "A|a" in {n["name"] for n in report["nets"]}


def test_pex_surfaces_case_collision_before_simulation(tmp_path, monkeypatch):
    import klayout_tools.pex as pex_module
    from klayout_tools.pex import PexError, run_pex
    from test_pex import _write_request, _write_schematic_dut, _write_testbench

    def _unreachable(*_args, **_kwargs):
        raise AssertionError("run_sim must not run after a failed extraction")

    monkeypatch.setattr(pex_module, "run_sim", _unreachable)
    tb = _write_testbench(
        tmp_path / "tb.spice", _write_schematic_dut(tmp_path / "d.sp")
    )
    request = _write_request(tmp_path / "request.json", tb)
    with pytest.raises(PexError, match="extraction failed") as exc_info:
        run_pex(_colliding_gds(tmp_path), [str(request)], "sky130")
    _assert_names_both(exc_info)
