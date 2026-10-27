"""`klt extract` / `klt lvs` case-fold net-name collisions (issue #4200)."""

from __future__ import annotations

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
