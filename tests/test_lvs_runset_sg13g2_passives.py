"""Issue #7855: SG13G2 ``cap_cmim`` and poly-resistor ``X`` calls become native
``C``/``R`` cards, and the converted reference stays readable for the census
and the unresolved-call scan.

Deck-free: the text conversion and the reader need neither the standalone
``klayout`` binary nor the PDK. The same behaviour against the pinned
``sg13g2.lvs`` is in ``tests/test_lvs_runset_sg13g2_cards.py``."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

import test_lvs_runset_reference as reference_tests
from klayout_tools import lvs_runset_census
from klayout_tools.lvs import _unresolved_reference_subckt_calls
from klayout_tools.lvs_runset_reference import (
    _unresolved_reference_calls,
    reference_spice_reader,
)

_schem = reference_tests._schem
_sg13g2_text = reference_tests._sg13g2_text
tmp_path = reference_tests.tmp_path  # pytest collects the fixture by module-level name


@pytest.mark.parametrize(
    ("call", "card"),
    [
        # The symbol's ``lvs_format``: ``C@name @pinlist @model w= l= m=``.
        (
            "XC1 plus minus cap_cmim w=6.99u l=6.99u m=2",
            "CXC1 plus minus cap_cmim w=6.99u l=6.99u m=2",
        ),
        # ``m`` omitted takes the symbol's default; keys fold to lower case.
        ("Xc2 a b cap_cmim W=7u L=8u", "CXc2 a b cap_cmim w=7u l=8u m=1"),
        # Three terminals ``A B <body>``; ``b``/``ps`` are carried as written.
        (
            "XR1 n1 n2 sub rppd w=0.5u l=0.5u m=1 b=0",
            "RXR1 n1 n2 sub rppd w=0.5u l=0.5u m=1 b=0",
        ),
        (
            "XR2 n1 n2 sub rppd W=5.85u L=11.25u b=3 ps=0.2u",
            "RXR2 n1 n2 sub rppd w=5.85u l=11.25u m=1 b=3 ps=0.2u",
        ),
        # Never defaulted: no ``b``/``ps`` on the call, none on the card.
        ("XR3 n1 n2 nw rhigh w=0.5u l=0.96u", "RXR3 n1 n2 nw rhigh w=0.5u l=0.96u m=1"),
        (
            "XR4 n1 n2 sub rsil\n+ PARAMS: w=0.5u l=0.5u m=4 b=1",
            "RXR4 n1 n2 sub rsil w=0.5u l=0.5u m=4 b=1",
        ),
    ],
)
def test_passive_call_becomes_the_native_card(tmp_path, call, card):
    converted = _sg13g2_text(tmp_path, _schem(call)).splitlines()
    assert card in converted
    # None of what the generic conversion wrote: a split-by-``m`` card, an
    # ``A=``/``P=`` rewrite, a positional ``0`` before the model, or the call.
    for line in converted:
        assert not line.upper().startswith("X"), line
        assert " 0 " not in line and "A=" not in line.upper(), line
        assert "_m" not in line.split()[0], line


@pytest.mark.parametrize(
    "call",
    [
        "XC1 a b cap_cmim l=7u",  # no w
        "XC1 a b cap_cmim w=7u",  # no l
        "XC1 a b cap_cmim w= l=7u",  # empty w
        "XC1 a b c cap_cmim w=7u l=7u",  # three terminals
        "XR1 a b rppd w=1u l=1u",  # two terminals
        "XR1 a b sub rppd w=1u",  # no l
        "XR1 a b c sub rhigh w=1u l=1u",  # four terminals
    ],
)
def test_unconvertible_passive_call_stays_a_call(tmp_path, call):
    # No guessed ``w``/``l``/body: left as written, for the scan to name.
    assert call in _sg13g2_text(tmp_path, _schem(call)).splitlines()


def test_passive_hierarchy_and_explicit_device_map_take_precedence(tmp_path):
    text = (
        ".subckt SCHEM a b\n"
        "Xchild a b cap_cmim w=1u l=1u\n"
        "XR1 a b sub rppd w=1u l=1u\n"
        ".ends SCHEM\n"
        ".subckt cap_cmim a b w=1u l=1u\nR1 a b 1k\n.ends cap_cmim\n"
    )
    # A circuit the file defines under the primitive's name is hierarchy.
    converted = _sg13g2_text(tmp_path, text).splitlines()
    assert "Xchild a b cap_cmim w=1u l=1u" in converted
    assert "RXR1 a b sub rppd w=1u l=1u m=1" in converted
    # A caller's own binding for the callee wins, as for the other primitives.
    mapped = _sg13g2_text(
        tmp_path,
        _schem("XR1 a b sub rppd w=1u l=1u"),
        device_map={"rppd": {"kind": "resistor", "class": "my_res"}},
    )
    assert "RXR1 a b sub rppd" not in mapped
    assert "my_res" in mapped


def test_a_top_named_like_the_primitive_still_converts_its_own_call(tmp_path):
    # The PDK's ``cap_devices/cap_cmim`` unit circuit is called ``cap_cmim``.
    text = ".subckt cap_cmim\nXC1 a b cap_cmim w=1u l=1u m=3\n.ends cap_cmim\n"
    assert "CXC1 a b cap_cmim w=1u l=1u m=3" in _sg13g2_text(tmp_path, text)


# --- The reader the census and the unresolved-call scan share -------------

VALUELESS = (
    ".subckt top a b c d sub\n"
    "CXC1 a b cap_cmim w=6.99u l=6.99u m=2\n"
    "RXR1 a b sub rppd w=0.5u l=0.5u m=1 b=0\n"
    "LXL1 a b sub inductor w=5.3u s=8.9u\n"
    "LXL2 a b c sub inductor3 w=2u s=2.1u d=25.35u nr_r=2\n"
    "CXC2 a b c sub sg13_hv_svaricap w=3.74u l=0.3u Nx=2\n"
    ".ends top\n"
)


def _read(tmp_path, text, deck_name="sg13g2.lvs"):
    source = tmp_path / "reference_converted.sp"
    source.write_text(text)
    deck = tmp_path / deck_name
    deck.write_text("# runset\n")
    return str(source), str(deck)


def test_plain_reader_still_refuses_the_valueless_cards_for_other_decks(tmp_path):
    path, deck = _read(tmp_path, VALUELESS, "sky130.lvs")
    with pytest.raises(RuntimeError, match="find a value"):
        kdb.Netlist().read(path, reference_spice_reader(deck))


def test_sg13g2_reader_keeps_the_valueless_cards_as_devices(tmp_path):
    path, deck = _read(tmp_path, VALUELESS)
    netlist = kdb.Netlist()
    netlist.read(path, reference_spice_reader(deck))
    (top,) = netlist.each_circuit()
    shapes = sorted(
        (d.device_class().name, len(d.device_class().terminal_definitions()))
        for d in top.each_device()
    )
    assert shapes == [
        ("CAP_CMIM", 2),
        ("INDUCTOR", 3),
        ("INDUCTOR3", 4),
        ("RPPD", 3),
        ("SG13_HV_SVARICAP", 4),
    ]


@pytest.mark.parametrize(
    ("element", "pins", "model"),
    [
        ("R", "1 2", "rppd"),
        ("R", "1 2", "rhigh"),
        ("R", "1 2", "rsil"),
        ("L", "1 2", "inductor"),
        ("L", "1 2 3", "inductor3"),
        ("C", "1 2 3", "sg13_hv_svaricap"),
        ("C", "1", "cap_cmim"),
    ],
)
@pytest.mark.parametrize("bodies", [("0", "sub"), ("sub", "0"), ("sub", "7")])
def test_numeric_passive_terminals_retain_their_connections(
    tmp_path, element, pins, model, bodies
):
    cards = [
        f"{element}{i} {pins} {body} {model} w=1u" for i, body in enumerate(bodies, 1)
    ]
    path, deck = _read(tmp_path, ".subckt top\n" + "\n".join(cards) + "\n.ends\n")
    netlist = kdb.Netlist()
    netlist.read(path, reference_spice_reader(deck))
    connections = {
        d.name: [
            d.net_for_terminal(t.id()).name
            for t in d.device_class().terminal_definitions()
        ]
        for d in netlist.circuit_by_name("TOP").each_device()
        if d.device_class().name == model.upper()
    }
    assert connections == {
        str(i): [*pins.split(), body.upper()] for i, body in enumerate(bodies, 1)
    }


def test_sg13g2_reader_preserves_explicit_resistor_values(tmp_path):
    path, deck = _read(
        tmp_path,
        ".subckt top\nR1 a b 0 rppd R=17\nR2 c d 250 $SUB=sub rhigh\n.ends\n",
    )
    netlist = kdb.Netlist()
    netlist.read(path, reference_spice_reader(deck))
    assert {
        d.device_class().name: d.parameter("R")
        for d in netlist.circuit_by_name("TOP").each_device()
    } == {"RPPD": 17, "RHIGH": 250}


def test_grounded_wrapped_resistor_beside_native_valued_cdl(tmp_path):
    path, deck = _read(
        tmp_path,
        ".subckt top\nR1 a b 0 $[rppd] w=1u l=1u\n"
        "R2 c d sub $[rppd] w=1u l=1u\n"
        "R3 e f 260 $SUB=sub $[rppd] w=1u l=1u\n.ends\n",
    )
    assert lvs_runset_census._reference_source_census(path, deck) == {"RPPD": 3}


def test_census_counts_the_converted_passives(tmp_path):
    path, deck = _read(tmp_path, VALUELESS)
    verdict = {"device_census": {}}
    lvs_runset_census._attach_reference_source(verdict, path, deck)
    assert verdict["device_census"] == {
        "reference_source": {
            "CAP_CMIM": 1,
            "INDUCTOR": 1,
            "INDUCTOR3": 1,
            "RPPD": 1,
            "SG13_HV_SVARICAP": 1,
        },
        "reference_source_subcircuits": {},
    }


def test_census_stays_none_for_a_card_nothing_can_read(tmp_path):
    # One net: no stand-in value makes this a device. Fail closed, as before.
    path, deck = _read(tmp_path, ".subckt top a\nRXR1 a w=1u l=1u\n.ends\n")
    assert lvs_runset_census._reference_source_census(path, deck) is None
    assert (
        _unresolved_reference_calls(
            path, "top", {}, deck, _unresolved_reference_subckt_calls
        )
        == []
    )


def test_unresolved_call_is_reported_beside_a_valueless_card(tmp_path):
    # The scan used to return ``[]`` here: the reader raised on the cap card
    # before it reached the call.
    text = (
        ".subckt top a b c sub\n"
        "CXC1 a b cap_cmim w=6.99u l=6.99u m=1\n"
        "XQ9 a b c sub npn13G2v Nx=1\n"
        ".ends top\n"
    )
    path, deck = _read(tmp_path, text)
    found = _unresolved_reference_calls(
        path, "top", {}, deck, _unresolved_reference_subckt_calls
    )
    assert [
        (m["instance"]["reference"], m["subcircuit"]["reference"]) for m in found
    ] == [("Q9", "NPN13G2V")]
    assert json.dumps(found)  # JSON-contract: a plain, serialisable finding list
    # Another deck's reader is unchanged: it still reads the file as unreadable.
    path, deck = _read(tmp_path, text, "sky130.lvs")
    assert (
        _unresolved_reference_calls(
            path, "top", {}, deck, _unresolved_reference_subckt_calls
        )
        == []
    )
