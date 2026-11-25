"""Unnamed device classes in the ``klayout-runset`` census (issue #5407)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import lvs_runset_census
from klayout_tools.coverage import coverage_validation_error


def _netlist_with_classes(*names: str) -> kdb.Netlist:
    nl = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    nl.add(circuit)
    for name in names:
        dc = kdb.DeviceClassMOS4Transistor()
        dc.name = name
        nl.add(dc)
        circuit.create_device(dc, "D")
    return nl


def test_unnamed_device_class_is_reported_not_raised():
    layout = _netlist_with_classes("NMOS", "")
    ref = _netlist_with_classes("NMOS")
    ld, lc, lu = lvs_runset_census._class_census(layout)
    rd, rc, ru = lvs_runset_census._class_census(ref)
    assert (lc, lu, ru) == ({"NMOS": 1}, 1, 0)
    assert "" not in ld
    cov = lvs_runset_census._build_coverage(ld, rd, lc, rc, lu + ru)
    assert coverage_validation_error(cov) in (None, "")
    assert cov["checked"] == ["NMOS"]
    assert {"id": "<unnamed>", "reason": "unnamed_device_class"} in cov["skipped"]
    assert cov["unnamed_device_instances"] == 1


def test_all_unnamed_classes_still_nothing_checked():
    nl = _netlist_with_classes("")
    d, c, u = lvs_runset_census._class_census(nl)
    assert (d, c, u) == ([], {}, 1)
    cov = lvs_runset_census._build_coverage(d, d, c, c, u)
    assert cov["nothing_checked"] is True
    assert coverage_validation_error(cov) in (None, "")


def _write_spice(path, text: str) -> str:
    path.write_text(text)
    return str(path)


_DECK_TEXT = 'DEVICE_LIST = ["pdk__fet", "pdk__res"]\n'


def test_reference_source_census_counts_in_scope_cards_per_definition(tmp_path):
    """Issue #6902: the evidence counts, upper-cased, every ``DEVICE_LIST``
    subcircuit call plus every device card the plain reader keeps, over
    every circuit definition -- the same unit ``_class_census`` counts, so
    the totals compare."""
    deck = _write_spice(tmp_path / "pdk.lvs", _DECK_TEXT)
    ref = _write_spice(
        tmp_path / "ref.spice",
        "*.subckt top\n"
        "XM1 a b c pdk__fet L=0.5 W=2\n"
        "XR1 a b pdk__res L=2\n"
        "C1 a b pdk__cap_mim C=1\n"
        "XSUB a b helper\n"
        "*.subckt helper\n"
        "XM2 d e f pdk__fet L=0.5 W=1\n"
        "*.ends helper\n"
        "*.ends top\n",
    )

    counts = lvs_runset_census._reference_source_census(ref, deck)

    # XSUB is out of scope (helper is not in DEVICE_LIST); the two in-scope
    # fet calls count one each per definition; the modelled C card counts
    # under its model name.
    assert counts == {
        "PDK__FET": 2,
        "PDK__RES": 1,
        "PDK__CAP_MIM": 1,
    }


def test_reference_source_census_folds_case_like_the_census_sides(tmp_path):
    deck = _write_spice(tmp_path / "pdk.lvs", _DECK_TEXT)
    ref = _write_spice(
        tmp_path / "ref.spice",
        "XM1 a b c PDK__FET L=0.5 W=2\n",
    )

    assert lvs_runset_census._reference_source_census(ref, deck) == {"PDK__FET": 1}


def test_reference_source_census_folds_the_cdl_model_wrapper(tmp_path):
    """Issue #7855: the PDK's native CDL spells a few cards ``$[model]``. The
    reader strips that wrapper from the converted reference, so the source
    census counts those cards under the plain model -- never as a class of
    their own, which signoff would report as devices the reference lacks."""
    deck = _write_spice(tmp_path / "pdk.lvs", _DECK_TEXT)
    ref = _write_spice(
        tmp_path / "ref.spice",
        "Rp1 n1 n2 260 $SUB=sub $[pdk__res] m=1 l=0.5u w=0.5u\n"
        "Rp2 n3 n4 260 $SUB=sub pdk__res m=1 l=0.5u w=0.5u\n"
        "C0 p m 74f $[pdk__cap] w=7u l=7u m=1\n"
        "C1 p m 74f pdk__cap w=7u l=7u m=1\n",
    )

    assert lvs_runset_census._reference_source_census(ref, deck) == {
        "PDK__CAP": 2,
        "PDK__RES": 2,
    }


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("rppd", "RPPD"),
        ("$[rppd]", "RPPD"),
        ("$[RPPD]", "RPPD"),
        ("$[cap_cmim]", "CAP_CMIM"),
        ("", ""),
        (None, ""),
        # Only a whole-name wrapper is CDL notation; anything else is a name.
        ("a$[b]", "A$[B]"),
        ("$[]", "$[]"),
    ],
)
def test_device_class_key_folds_case_and_the_cdl_wrapper(name, key):
    assert lvs_runset_census._device_class_key(name) == key


def test_reference_source_census_is_none_when_unreadable(tmp_path):
    deck = _write_spice(tmp_path / "pdk.lvs", _DECK_TEXT)
    assert lvs_runset_census._reference_source_census(None, deck) is None
    assert (
        lvs_runset_census._reference_source_census(str(tmp_path / "absent.spice"), deck)
        is None
    )
    assert (
        lvs_runset_census._reference_source_census(str(tmp_path / "ref.spice"), None)
        is None
    )


def test_source_subcircuit_count_excludes_primitive_with_same_model(tmp_path):
    deck = _write_spice(tmp_path / "pdk.lvs", _DECK_TEXT)
    ref = _write_spice(
        tmp_path / "ref.spice",
        "* reference\nXM1 d g s b pdk__fet L=0.5 W=2\nM2 d g s b pdk__fet L=0.5 W=2\n",
    )
    verdict = {"device_census": {}}
    lvs_runset_census._attach_reference_source(verdict, ref, deck)
    assert verdict["device_census"] == {
        "reference_source": {"PDK__FET": 2},
        "reference_source_subcircuits": {"PDK__FET": 1},
    }
