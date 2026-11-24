"""`klt extract` case-alias ``merged_net_labels`` warning wording (issue #7724).

A net labelled twice with spellings that differ only by case (``A`` / ``a``)
is merged into ``A|a``. The default spelling is a documented contract (#696),
so only the ``warnings[]`` prose changes: it names ``--rename-declared-pins``
(the existing fix, #2854) instead of claiming two differently-named nets were
shorted. A merge of genuinely different labels keeps today's text exactly.
"""

from __future__ import annotations

from klayout_tools.extract import run_extract
from klayout_tools.extract_warnings import merged_net_label_warnings
from test_extract import _make_inverter_layout, _write_gds

# The warning text for a merge of genuinely different labels, frozen from
# before #7724. `Y|Y2` is not a case alias, so this must stay byte-identical.
_DIFFERENT_LABELS_WARNING = (
    "net 'Y|Y2' merges 2 distinct labels (Y, Y2) onto one net -- KLayout "
    "joins multiple labels found on the same electrical net (as '|' here "
    "and in the written netlist, matching NetlistSpiceWriter's own "
    "node-name escaping); this usually means two differently-named nets "
    "were shorted together in the layout -- see docs/cli/extract.md's "
    "'Merged net labels' section."
)

_DECLARED_PINS = frozenset({"A", "Y", "VGND", "VPWR", "VPB"})


def _entry(*labels: str) -> dict[str, object]:
    return {"net": "|".join(labels), "labels": list(labels)}


# --------------------------------------------------------------------------- #
# Pure builder: which merges count as a case alias
# --------------------------------------------------------------------------- #


def test_case_alias_warning_keeps_prefix_and_names_the_flag():
    (warning,) = merged_net_label_warnings([_entry("A", "a")])

    assert warning.startswith(
        "net 'A|a' merges 2 distinct labels (A, a) onto one net"
    ), warning
    assert "differ only in letter case" in warning
    assert "--rename-declared-pins" in warning
    assert "--pins" in warning and "--def-pins" in warning
    assert "pin_renames.renamed" in warning
    assert "docs/cli/extract.md" in warning


def test_case_alias_warning_drops_the_shorted_nets_claim():
    (warning,) = merged_net_label_warnings([_entry("A", "a")])

    assert "differently-named" not in warning
    assert "shorted" not in warning


def test_case_alias_warning_is_not_the_cross_net_case_collision_warning():
    # "differ only by case" marks the *cross-net* case-collision warning
    # (#4200/#7091: two distinct nets `PB`/`pb`, which merge on SPICE
    # read-back). One net labelled twice is a different, milder finding, and
    # callers tell the two apart by that phrase (see
    # tests/test_extract_case_collision.py), so this line must not carry it.
    (warning,) = merged_net_label_warnings([_entry("A", "a")])

    assert "differ only by case" not in warning
    assert "differing only by case" not in warning


def test_three_case_spellings_are_one_case_alias():
    (warning,) = merged_net_label_warnings([_entry("VDD", "vdd", "Vdd")])

    assert warning.startswith(
        "net 'VDD|vdd|Vdd' merges 3 distinct labels (VDD, vdd, Vdd) onto one net"
    ), warning
    assert "--rename-declared-pins" in warning
    assert "differently-named" not in warning


def test_different_labels_keep_byte_identical_text():
    assert merged_net_label_warnings([_entry("Y", "Y2")]) == [_DIFFERENT_LABELS_WARNING]


def test_partial_case_alias_is_not_a_pure_case_alias():
    # `Y` / `y` differ only by case, but `Y2` is a genuinely different label,
    # so the "two differently-named nets were shorted" advice still applies.
    (warning,) = merged_net_label_warnings([_entry("Y", "Y2", "y")])

    assert warning.startswith(
        "net 'Y|Y2|y' merges 3 distinct labels (Y, Y2, y) onto one net"
    ), warning
    assert "--rename-declared-pins" not in warning
    assert "differently-named nets were shorted together" in warning


def test_identical_labels_are_not_called_a_case_alias():
    # Not reachable from a real extraction, but "differ only in letter case" would be
    # false for two byte-equal labels, so they take the generic wording.
    (warning,) = merged_net_label_warnings([_entry("A", "A")])

    assert "differ only in letter case" not in warning
    assert "--rename-declared-pins" not in warning


def test_each_entry_is_worded_independently():
    warnings = merged_net_label_warnings([_entry("A", "a"), _entry("Y", "Y2")])

    assert len(warnings) == 2
    assert "--rename-declared-pins" in warnings[0]
    assert warnings[1] == _DIFFERENT_LABELS_WARNING


def test_no_merges_no_warnings():
    assert merged_net_label_warnings([]) == []


# --------------------------------------------------------------------------- #
# End to end through run_extract: warning text only, netlist unchanged
# --------------------------------------------------------------------------- #


def test_extract_case_alias_warning_and_unchanged_netlist(tmp_path):
    path = _write_gds(_make_inverter_layout(extra_a_label="a"), tmp_path / "alias.gds")
    netlist_path = tmp_path / "alias.spice"
    report = run_extract(
        path,
        "sky130",
        output=str(netlist_path),
        declared_pins=_DECLARED_PINS,
        parasitics=True,
    )

    # The structured data and the written netlist keep the documented
    # `NAME|alias` spelling (#696): only the prose changed.
    assert report["merged_net_labels"] == [{"net": "A|a", "labels": ["A", "a"]}]
    assert "A|a" in [n["name"] for n in report["nets"]]
    subckt = next(
        ln
        for ln in netlist_path.read_text().splitlines()
        if ln.upper().startswith(".SUBCKT")
    )
    assert "A|a" in subckt.split()[2:]

    (warning,) = [w for w in report["warnings"] if "merges 2 distinct labels" in w]
    assert warning.startswith(
        "net 'A|a' merges 2 distinct labels (A, a) onto one net"
    ), warning
    assert "differ only in letter case" in warning
    assert "--rename-declared-pins" in warning
    assert "shorted" not in warning


def test_extract_different_label_merge_text_is_unchanged(tmp_path):
    path = _write_gds(_make_inverter_layout(extra_y_label="Y2"), tmp_path / "diff.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "diff.spice"))

    assert report["merged_net_labels"] == [{"net": "Y|Y2", "labels": ["Y", "Y2"]}]
    assert [w for w in report["warnings"] if "merges" in w] == [
        _DIFFERENT_LABELS_WARNING
    ]


def test_extract_rename_declared_pins_clears_the_merge_the_warning_points_at(tmp_path):
    # The flag the new warning names really does what it says: no `|` node,
    # no merge warning, the alias recorded in `pin_renames.renamed`.
    path = _write_gds(_make_inverter_layout(extra_a_label="a"), tmp_path / "fix.gds")
    netlist_path = tmp_path / "fix.spice"
    report = run_extract(
        path,
        "sky130",
        output=str(netlist_path),
        declared_pins=_DECLARED_PINS,
        parasitics=True,
        rename_declared_pins=True,
    )

    assert report["merged_net_labels"] == []
    assert "|" not in netlist_path.read_text()
    assert report["pin_renames"]["renamed"] == [{"from": "A|a", "to": "A"}]
