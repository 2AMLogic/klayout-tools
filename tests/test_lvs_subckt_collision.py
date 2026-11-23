"""Issue #5665: a reference `.SUBCKT` named after a deck device class.

Split out of `tests/test_lvs.py` (which is at its per-file size ceiling) —
see the `#5665` banner below for the defect narrative and
`netlist_capacitor_recovery.py`'s module docstring for the mechanism.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


# --------------------------------------------------------------------------- #
# Issue #5665: a reference `.SUBCKT` whose *name* collides with a drawn-
# resistor or MoM-capacitor class in the resolved deck's recovery tables.
# The #1157/#1942 recovery reader claimed such a name unconditionally in
# `wants_subcircuit`, telling KLayout it was a device model -- so the reader
# silently swallowed the matching `.SUBCKT` *definition* and a one-
# subcircuit reference came back with zero circuits ("reference netlist has
# no top circuit"; or, when the file also instantiated it via an `X` card,
# "Not a known element type: 'X'"). A foundry resistor CDL is conventionally
# named after the device it models, so the collision is the common case.
# --------------------------------------------------------------------------- #


#: ``(deck, colliding `.SUBCKT` name, plain-element card, subckt-call card)``.
#: A drawn-resistor class on each of two decks (proving the fix is not
#: deck-keyed) plus sg13g2's MoM capacitor (the `custom_device_classes`
#: lookup rather than the resistor one). The plain-element card carries a
#: real value token so the default reader accepts it without conversion;
#: the subckt-call card is the valueless foundry shape of the original
#: report. The inner card names the colliding class as its model so the
#: surviving device's class binding can be asserted too (the MoM leg has no
#: native element letter, so its card stays a plain valued `C` and no class
#: binding is expected).
_ISSUE_5665_COLLISIONS = [
    pytest.param(
        "sg13g2",
        "rppd",
        "R1 net5 net6 sub 260 rppd l=1.2u w=0.7u",
        "Rp3 net5 net6 sub rppd m=1 l=1.2u w=0.7u",
        True,
        id="sg13g2-rppd",
    ),
    pytest.param(
        "sg13g2",
        "cap_cmomi",
        "C1 a b 1e-15",
        "C1 a b 1e-15",
        False,
        id="sg13g2-cap_cmomi",
    ),
    pytest.param(
        "gf180mcu",
        "ppolyf_u_3k",
        "R1 n1 n2 260 ppolyf_u_3k l=1u w=1u",
        "R1 n1 n2 ppolyf_u_3k w=1u l=1u",
        True,
        id="gf180mcu-ppolyf_u_3k",
    ),
]


@pytest.mark.parametrize("form", ["plain-element", "subckt-call"])
@pytest.mark.parametrize(
    ("deck", "subckt_name", "plain_card", "subckt_call_card", "model_named"),
    _ISSUE_5665_COLLISIONS,
)
def test_reference_subckt_named_after_deck_device_class_survives_the_read(
    tmp_path, form, deck, subckt_name, plain_card, subckt_call_card, model_named
):
    """The recovery reader must not claim a name the reference itself
    defines: the circuit survives (exactly one circuit, one top circuit --
    pre-fix both were zero/empty) and the device inside keeps its class
    binding instead of the whole definition being swallowed."""
    from klayout_tools.lvs import _read_reference_netlist

    card = subckt_call_card if form == "subckt-call" else plain_card
    path = _write(
        tmp_path / f"collide_{deck}_{subckt_name}.cdl",
        f".SUBCKT {subckt_name}\n{card}\n.ENDS {subckt_name}\n",
    )
    netlist = _read_reference_netlist(path, form=form, deck=deck)

    circuits = list(netlist.each_circuit())
    assert len(circuits) == 1  # pre-fix: 0 -- the definition was swallowed
    assert [c.name for c in netlist.top_circuits()] == [subckt_name.upper()]
    devices = list(circuits[0].each_device())
    assert len(devices) == 1
    if model_named:
        # Rescued without downgrading: the model-named card still binds the
        # deck class (uppercased by the SPICE reader), not a generic class.
        assert devices[0].device_class().name == subckt_name.upper()


@pytest.mark.parametrize(
    ("deck", "subckt_name", "plain_card", "subckt_call_card", "model_named"),
    _ISSUE_5665_COLLISIONS,
)
def test_reference_subckt_named_after_deck_device_class_mixed_case(
    tmp_path, deck, subckt_name, plain_card, subckt_call_card, model_named
):
    """The not-claimed-because-defined check is case-insensitive in both
    directions: a mixed-case `.SUBCKT RPpd` definition survives the same
    uppercase-keyed claim (`defined_subckt_names` upper-cases, exactly like
    the lookups do)."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / f"collide_mixed_{deck}.cdl",
        f".SUBCKT {subckt_name.capitalize()}\n{plain_card}\n"
        f".ENDS {subckt_name.capitalize()}\n",
    )
    netlist = _read_reference_netlist(path, deck=deck)

    assert len(list(netlist.each_circuit())) == 1
    assert [c.name for c in netlist.top_circuits()] == [subckt_name.upper()]


def test_reference_subckt_named_after_deck_class_without_deck_unchanged(tmp_path):
    """With no `deck` at all both recovery lookups are empty, so a colliding
    `.SUBCKT rppd` was never claimed -- that read must stay byte-identical
    (the fix is a no-op without a deck)."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "collide_no_deck.cdl",
        ".SUBCKT rppd\nR1 net5 net6 sub 260 rppd l=1.2u w=0.7u\n.ENDS rppd\n",
    )
    netlist = _read_reference_netlist(path)

    assert [c.name for c in netlist.top_circuits()] == ["RPPD"]


def test_reference_defining_and_instantiating_colliding_subckt_parses(tmp_path):
    """The define-plus-`X`-card shape: a reference that *defines* `.SUBCKT
    rppd` and instantiates it via an `X` card is a legitimate hierarchical
    reference -- pre-fix it did not parse at all ("Not a known element type:
    'X'"). Post-fix it reads as a TOP -> RPPD hierarchy with the `X` card a
    real subcircuit call."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "define_and_call.cdl",
        ".SUBCKT rppd A B SUB\n"
        "R1 A B SUB 260 rppd l=1.2u w=0.7u\n"
        ".ENDS rppd\n"
        ".SUBCKT TOP A B SUB\n"
        "X1 A B SUB rppd\n"
        ".ENDS TOP\n",
    )
    netlist = _read_reference_netlist(path, deck="sg13g2")

    top = next(c for c in netlist.top_circuits() if c.name == "TOP")
    calls = list(top.each_subcircuit())
    assert len(calls) == 1
    assert calls[0].circuit_ref().name == "RPPD"


def test_reference_undefined_resistor_x_card_still_recovers_a_device(tmp_path):
    """Regression guard for #1157/#1942: an `X` card naming a drawn-resistor
    class the file does *not* define must still be recovered as a device of
    that class -- the not-claimed check only yields for names the file
    itself defines."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "undefined_x.cdl",
        ".SUBCKT TOP A B SUB\nX$1 A B SUB res_high_po r=3627.9776 L=10U W=1U\n"
        ".ENDS TOP\n",
    )
    netlist = _read_reference_netlist(path, deck="sky130")

    top = next(c for c in netlist.top_circuits() if c.name == "TOP")
    devices = list(top.each_device())
    assert len(devices) == 1
    assert devices[0].device_class().name == "res_high_po"
    # And no phantom circuit was synthesised for the undefined call.
    assert [c.name for c in netlist.each_circuit()] == ["TOP"]


def test_reference_included_resistor_definition_is_a_top_circuit(tmp_path):
    from klayout_tools.lvs import _read_reference_netlist

    device = _write(
        tmp_path / "dev.cdl",
        ".SUBCKT rppd\nR1 net5 net6 sub 260 rppd l=1.2u w=0.7u\n.ENDS rppd\n",
    )
    path = _write(tmp_path / "top.cdl", f".include '{device}'\n")
    netlist = _read_reference_netlist(path, deck="sg13g2")

    (top,) = netlist.top_circuits()
    assert top.name == "RPPD"
    (resistor,) = top.each_device()
    assert resistor.device_class().name == "RPPD"


@pytest.mark.parametrize("side", ["layout", "plain-element", "subckt-call"])
def test_nested_include_preserves_hierarchy_and_recovers_undefined_device(
    tmp_path, side
):
    from klayout_tools.lvs import _read_reference_netlist, _resolve_layout

    lib = tmp_path / "lib"
    lib.mkdir()
    # KLayout resolves this path against the including file and detects gzip
    # from the content, even without a .gz suffix.
    (lib / "devices.cdl").write_text('.InC "MoM cell.cdl"\n')
    (lib / "MoM cell.cdl").write_bytes(
        gzip.compress(
            b".lib unused_corner\n.subckt rppd A B SUB\n.ends\n.endl\n"
            b".subckt Cap_Cmomi A B\nC1 A B 1f\n.ends\n"
        )
    )
    path = _write(
        tmp_path / "top.cdl",
        ".include lib/devices.cdl\n.subckt TOP A B SUB\n"
        "X1 A B cap_cmomi\nX$2 A B SUB rppd r=260 L=1.2U W=0.7U\n.ends\n",
    )
    if side == "layout":
        netlist = _resolve_layout(
            {"netlist": path, "deck": "sg13g2"}, str(tmp_path), False
        )[0]
    else:
        netlist = _read_reference_netlist(path, deck="sg13g2", form=side)

    (top,) = netlist.top_circuits()
    assert top.name == "TOP"
    (instance,) = top.each_subcircuit()
    assert instance.circuit_ref().name == "CAP_CMOMI"
    (capacitor,) = instance.circuit_ref().each_device()
    assert capacitor.device_class().name == "CAP"
    (resistor,) = top.each_device()
    assert resistor.device_class().name.upper() == "RPPD"


# --------------------------------------------------------------------------- #
# Issue #6705: the same collision reached through a corner-selected
# `.lib '<file>' <section>` line. KLayout's reader follows that directive
# (reading *only* the named section -- case-insensitively), but the
# recovery pre-scan did not, so a `.SUBCKT rppd` defined inside the
# selected section was still claimed as a device class and swallowed
# (`reference netlist has no top circuit`, #5665's symptom again).
# --------------------------------------------------------------------------- #

#: One library file: `rppd` (a sg13g2 drawn-resistor class name) inside
#: section `tt`, `other` inside `ff`, `outside` after both sections. A
#: `.lib '<file>' <selector>` line picks exactly one section (KLayout reads
#: nothing else from the file); a plain `.include` of it reads only what
#: sits outside every section.
_CORNERS_LIB = (
    ".lib tt\n"
    ".SUBCKT rppd net5 net6 sub\n"
    "R1 net5 net6 sub 260 rppd l=1.2u w=0.7u\n"
    ".ENDS rppd\n"
    ".endl\n"
    ".lib ff\n"
    ".SUBCKT other a b\n"
    "C1 a b 1f\n"
    ".ENDS other\n"
    ".endl\n"
    ".SUBCKT outside a b\n"
    "C1 a b 2f\n"
    ".ENDS outside\n"
)


@pytest.mark.parametrize("selector", ["tt", "TT"])
def test_reference_lib_selected_section_colliding_subckt_survives(tmp_path, selector):
    """The issue's reproduction: a quoted, space-containing, relative
    `.lib` line selecting the section that defines the colliding name. The
    definition survives as a circuit (pre-fix: zero circuits, `reference
    netlist has no top circuit`) and the device inside keeps its class
    binding. The selector matches its declaration case-insensitively, as
    KLayout does."""
    from klayout_tools.lvs import _read_reference_netlist

    _write(tmp_path / "device corners.lib", _CORNERS_LIB)
    path = _write(tmp_path / "top.cdl", f".lib 'device corners.lib' {selector}\n")
    netlist = _read_reference_netlist(path, deck="sg13g2")

    assert [c.name for c in netlist.each_circuit()] == ["RPPD"]
    (top,) = netlist.top_circuits()
    assert top.name == "RPPD"
    (resistor,) = top.each_device()
    assert resistor.device_class().name == "RPPD"


def test_reference_lib_two_sections_of_one_file_are_both_read(tmp_path):
    """One library selected twice, once per section: both sections'
    definitions count (the scan keys its visited set on file *and*
    section, so the second selection is not mistaken for a cycle)."""
    from klayout_tools.lvs import _read_reference_netlist

    _write(tmp_path / "device corners.lib", _CORNERS_LIB)
    path = _write(
        tmp_path / "top.cdl",
        ".lib 'device corners.lib' tt\n.lib 'device corners.lib' ff\n",
    )
    netlist = _read_reference_netlist(path, deck="sg13g2")

    assert sorted(c.name for c in netlist.each_circuit()) == ["OTHER", "RPPD"]


@pytest.mark.parametrize("where", ["other-section", "outside-sections"])
def test_reference_lib_colliding_definition_not_selected_keeps_x_card_recovery(
    tmp_path, where
):
    """The #1157/#1942 no-op guarantee: a colliding `.SUBCKT` that the
    selection does *not* reach (it sits in another section, or outside
    every section) is not a definition as far as the read is concerned, so
    an `X` card naming the deck class still recovers as a device."""
    from klayout_tools.lvs import _read_reference_netlist

    colliding = ".SUBCKT rppd A B SUB\nR1 A B SUB 260 rppd l=1.2u w=0.7u\n.ENDS rppd\n"
    ff_section = ".lib ff\n.SUBCKT other a b\nC1 a b 1f\n.ENDS other\n.endl\n"
    if where == "other-section":
        lib = f".lib tt\n{colliding}.endl\n{ff_section}"
    else:
        lib = f"{ff_section}{colliding}"
    _write(tmp_path / "corners.lib", lib)
    path = _write(
        tmp_path / "top.cdl",
        ".lib 'corners.lib' ff\n"
        ".SUBCKT TOP A B SUB\n"
        "X$1 A B SUB rppd r=260 L=1.2U W=0.7U\n"
        ".ENDS TOP\n",
    )
    netlist = _read_reference_netlist(path, deck="sg13g2")

    assert sorted(c.name for c in netlist.each_circuit()) == ["OTHER", "TOP"]
    top = next(c for c in netlist.top_circuits() if c.name == "TOP")
    (resistor,) = top.each_device()
    assert resistor.device_class().name.upper() == "RPPD"


def test_reference_lib_selected_section_include_resolves_relative(tmp_path):
    """An `.include`/`.inc` inside the selected section anchors at the
    containing *library* file's directory (like KLayout), not the top-level
    file's, and its colliding definition survives the same way."""
    from klayout_tools.lvs import _read_reference_netlist

    lib = tmp_path / "lib"
    lib.mkdir()
    _write(
        lib / "sibling.cdl",
        ".SUBCKT rppd A B SUB\nR1 A B SUB 260 rppd l=1.2u w=0.7u\n.ENDS rppd\n",
    )
    _write(lib / "corners.lib", ".lib tt\n.inc 'sibling.cdl'\n.endl\n")
    path = _write(tmp_path / "top.cdl", ".lib 'lib/corners.lib' tt\n")
    netlist = _read_reference_netlist(path, deck="sg13g2")

    (top,) = netlist.top_circuits()
    assert top.name == "RPPD"
    (resistor,) = top.each_device()
    assert resistor.device_class().name == "RPPD"


def test_recovery_scan_selects_named_lib_section(tmp_path):
    """Scan-level: the two-argument `.lib` form follows the file but keeps
    only the named section's names (selector case-insensitive), the same
    file may be selected per-section, a plain include still yields only
    what sits outside every section, and a gzip'd target is read."""
    from klayout_tools.netlist_normalize_scan import recovery_subckt_names

    (tmp_path / "corners.lib").write_bytes(gzip.compress(_CORNERS_LIB.encode()))
    path = _write(tmp_path / "top.cdl", ".lib 'corners.lib' tt\n")
    candidates = {"RPPD", "OTHER", "OUTSIDE"}

    assert recovery_subckt_names(".lib 'corners.lib' tt\n", path, candidates) == {
        "RPPD"
    }
    assert recovery_subckt_names(".lib 'corners.lib' TT\n", path, candidates) == {
        "RPPD"
    }
    assert recovery_subckt_names(".lib 'corners.lib' ff\n", path, candidates) == {
        "OTHER"
    }
    assert recovery_subckt_names(
        ".lib 'corners.lib' tt\n.lib 'corners.lib' ff\n", path, candidates
    ) == {"OTHER", "RPPD"}
    assert recovery_subckt_names(".include 'corners.lib'\n", path, candidates) == {
        "OUTSIDE"
    }


def test_reference_lib_missing_target_follows_include_error_path(tmp_path):
    """A missing `.lib` target is silent in the scan (the diagnostic stays
    with KLayout's reader, exactly like a missing `.include`)."""
    from klayout_tools.lvs import LvsError, _read_reference_netlist
    from klayout_tools.netlist_normalize_scan import recovery_subckt_names

    path = tmp_path / "top.cdl"
    text = ".lib 'missing.lib' tt\n.SUBCKT TOP a b\n.ENDS TOP\n"
    path.write_text(text)
    assert recovery_subckt_names(text, str(path), {"RPPD", "TOP"}) == {"TOP"}
    with pytest.raises(LvsError, match=r"could not parse reference netlist"):
        _read_reference_netlist(str(path), deck="sg13g2")


@pytest.mark.parametrize("side", ["layout", "reference"])
def test_included_inductor_definition_survives_device_recovery(tmp_path, side):
    from klayout_tools.lvs import _read_reference_netlist, _resolve_layout

    _write(
        tmp_path / "coil.cdl",
        ".subckt inductor A B SUB\nRcoil A B 12\n.ends\n",
    )
    path = _write(
        tmp_path / "top.cdl",
        ".include coil.cdl\n.subckt TOP P N SUB\nXcoil P N SUB inductor\n.ends\n",
    )
    if side == "layout":
        netlist = _resolve_layout(
            {"netlist": path, "deck": "sg13g2"}, str(tmp_path), False
        )[0]
    else:
        netlist = _read_reference_netlist(path, deck="sg13g2")
    (top,) = netlist.top_circuits()
    (instance,) = top.each_subcircuit()
    coil = instance.circuit_ref()
    assert coil.name == "INDUCTOR"
    (resistor,) = coil.each_device()
    assert resistor.device_class().name == "RES"


def test_recovery_scan_skips_cycles_and_leaves_missing_includes_to_klayout(tmp_path):
    from klayout_tools.lvs import LvsError, _read_reference_netlist
    from klayout_tools.netlist_normalize_scan import recovery_subckt_names

    path = tmp_path / "top.cdl"
    text = ".include lib.cdl\n"
    path.write_text(text)
    lib = tmp_path / "lib.cdl"
    lib.write_text(".inc top.cdl\n.subckt rppd\n.ends\n")
    assert recovery_subckt_names(text, str(path), {"RPPD", "CAP_CMOMI"}) == {"RPPD"}

    lib.unlink()
    with pytest.raises(LvsError, match=r"could not parse reference netlist.*lib\.cdl"):
        _read_reference_netlist(str(path), deck="sg13g2")


def test_nested_include_in_symlink_uses_real_file_directory(tmp_path):
    from klayout_tools.lvs import _read_reference_netlist

    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "entry.cdl").write_text(".inc leaf.cdl\n")
    (lib / "leaf.cdl").write_text(".subckt rppd A B\nR1 A B 10\n.ends\n")
    (tmp_path / "alias.cdl").symlink_to(lib / "entry.cdl")
    path = _write(tmp_path / "top.cdl", ".include 'alias.cdl'\n")
    netlist = _read_reference_netlist(path, deck="sg13g2")
    assert [c.name for c in netlist.top_circuits()] == ["RPPD"]
