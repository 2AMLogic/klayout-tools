"""Via/contact cut resistance and interconnect-resistance corner tables in the
curated parasitics decks (issue #3053).

Each expected value below is the magic tech file's published figure in
milliohm, copied by hand from the pinned PDK commit named in the deck
comments; the deck stores ohms (milliohm / 1000). When a local volare copy of
that PDK commit is present, the same values are also re-read from the real
tech file so a transcription error in either place fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from klayout_tools.decks import (
    HIGH_CORNER,
    LOW_CORNER,
    NOMINAL_CORNER,
    CutRC,
    LayerRC,
    ParasiticsDeck,
    describe_cut_resistance_gaps,
    get_extraction_deck,
    get_parasitics_deck,
)

PDK_COMMIT = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"

# deck -> cut name -> (lower_metal_index, nominal, high, low) in milliohm
PUBLISHED_CUTS_MOHM = {
    "gf180mcu": {
        "m2c": (0, 4500, 15000, 0),
        "m3c": (1, 4500, 15000, 0),
        "via3": (2, 4500, 15000, 0),
        "via4": (3, 4500, 15000, 0),
    },
    "sky130": {
        "mcon": (0, 9300, 23000, 1600),
        "m2c": (1, 4500, 15000, 2000),
        "m3c": (2, 3410, 8000, 500),
        "via3": (3, 3410, 8000, 500),
        "via4": (4, 380, 891, 12),
    },
}

# deck -> per-metal (nominal, high, low) sheet resistance in milliohm/sq
PUBLISHED_METALS_MOHM = {
    "gf180mcu": [
        (90, 104, 76),
        (90, 104, 76),
        (90, 104, 76),
        (90, 104, 76),
        (40, 49, 31),
    ],
    "sky130": [
        (12800, 17000, 10500),
        (125, 145, 105),
        (125, 145, 105),
        (47, 56, 38),
        (47, 56, 38),
        (29, 36, 21),
    ],
}

TECH_FILES = {
    "gf180mcu": "gf180mcu/versions/{c}/gf180mcuD/libs.tech/magic/gf180mcuD.tech",
    "sky130": "sky130/versions/{c}/sky130A/libs.tech/magic/sky130A.tech",
}


@pytest.mark.parametrize("deck_name", sorted(PUBLISHED_CUTS_MOHM))
def test_cut_rows_match_published_values(deck_name):
    deck = get_parasitics_deck(deck_name)
    expected = PUBLISHED_CUTS_MOHM[deck_name]
    assert {c.name for c in deck.cuts} == set(expected)
    for cut in deck.cuts:
        idx, nom, high, low = expected[cut.name]
        assert cut.lower_metal_index == idx
        assert cut.nominal_ohm == pytest.approx(nom / 1000)
        assert cut.high_ohm == pytest.approx(high / 1000)
        assert cut.low_ohm == pytest.approx(low / 1000)
        assert cut.resistance(NOMINAL_CORNER) == cut.nominal_ohm
        assert cut.resistance(HIGH_CORNER) == cut.high_ohm
        assert cut.resistance(LOW_CORNER) == cut.low_ohm


@pytest.mark.parametrize("deck_name", sorted(PUBLISHED_METALS_MOHM))
def test_metal_corners_match_published_values(deck_name):
    deck = get_parasitics_deck(deck_name)
    expected = PUBLISHED_METALS_MOHM[deck_name]
    assert len(deck.metals) == len(expected)
    for rc, (nom, high, low) in zip(deck.metals, expected, strict=True):
        assert rc.sheet_res(NOMINAL_CORNER) == pytest.approx(nom / 1000)
        assert rc.sheet_res(HIGH_CORNER) == pytest.approx(high / 1000)
        assert rc.sheet_res(LOW_CORNER) == pytest.approx(low / 1000)


@pytest.mark.parametrize("deck_name", sorted(PUBLISHED_CUTS_MOHM))
def test_cut_table_covers_every_adjacent_metal_pair(deck_name):
    ext = get_extraction_deck(deck_name)
    par = get_parasitics_deck(deck_name)
    assert describe_cut_resistance_gaps(par, len(ext.metals)) == []
    assert len(par.metals) == len(ext.metals)


def test_gf180mcu_high_corner_is_3p3x_nominal_and_low_is_ideal_short():
    for cut in get_parasitics_deck("gf180mcu").cuts:
        assert cut.high_ohm / cut.nominal_ohm == pytest.approx(15 / 4.5)
        # Published 0 ohm is stored as published: not clamped, not None.
        assert cut.low_ohm == 0.0
        assert cut.resistance(LOW_CORNER) == 0.0


def test_sky130_cuts_are_not_uniform():
    assert len({c.nominal_ohm for c in get_parasitics_deck("sky130").cuts}) > 1


@pytest.mark.parametrize("deck_name", ["sg13g2", "sg13cmos5l"])
def test_uncurated_decks_report_every_pair_as_a_gap(deck_name):
    ext = get_extraction_deck(deck_name)
    par = get_parasitics_deck(deck_name)
    assert par.cuts == ()
    gaps = describe_cut_resistance_gaps(par, len(ext.metals))
    assert gaps == [
        {"lower_metal_index": i, "upper_metal_index": i + 1}
        for i in range(len(ext.metals) - 1)
    ]
    assert gaps


def test_default_deck_has_no_cut_data_and_unpublished_corners_are_none():
    deck = ParasiticsDeck()
    assert deck.cuts == ()
    assert describe_cut_resistance_gaps(deck, 1) == []
    assert describe_cut_resistance_gaps(deck, 3) == [
        {"lower_metal_index": 0, "upper_metal_index": 1},
        {"lower_metal_index": 1, "upper_metal_index": 2},
    ]
    rc = LayerRC(sheet_res_ohm_sq=1.0, cap_area_ff_um2=0.0, cap_perim_ff_um=0.0)
    assert rc.sheet_res(HIGH_CORNER) is None
    assert rc.sheet_res(LOW_CORNER) is None
    assert CutRC("v", 0, nominal_ohm=1.0).resistance(HIGH_CORNER) is None


def test_unknown_corner_is_rejected():
    with pytest.raises(ValueError, match="unknown resistance corner 'typ'"):
        CutRC("v", 0, nominal_ohm=1.0).resistance("typ")


def test_gap_report_skips_covered_pairs():
    deck = ParasiticsDeck(cuts=(CutRC("v", 1, nominal_ohm=1.0),))
    assert describe_cut_resistance_gaps(deck, 4) == [
        {"lower_metal_index": 0, "upper_metal_index": 1},
        {"lower_metal_index": 2, "upper_metal_index": 3},
    ]


def _tech_file(deck_name: str) -> Path | None:
    for root in (
        Path.home() / ".ciel" / "ciel",
        Path.home() / ".volare" / "volare",
        Path.home() / ".pdk-cache" / "volare",
    ):
        path = root / TECH_FILES[deck_name].format(c=PDK_COMMIT)
        if path.is_file():
            return path
    return None


def _contact_blocks(text: str) -> dict[str, dict[str, int]]:
    """Map corner -> {cut name: milliohm} from the extract section."""
    blocks: dict[str, dict[str, int]] = {}
    corner = None
    for line in text.splitlines():
        m = re.match(r"\s*variants\s+(\S+)", line)
        if m:
            v = m.group(1)
            if v.startswith("(hrhc)"):
                corner = HIGH_CORNER
            elif v.startswith("(lrhc)"):
                corner = LOW_CORNER
            elif v in ("()", "(),(orig),(si)"):
                corner = NOMINAL_CORNER
            else:
                corner = None
            continue
        m = re.match(r"\s*contact\s+(\S+)\s+(\d+)\s*$", line)
        if m and corner:
            for name in m.group(1).split(","):
                blocks.setdefault(corner, {})[name] = int(m.group(2))
    return blocks


@pytest.mark.parametrize("deck_name", sorted(PUBLISHED_CUTS_MOHM))
def test_cut_rows_match_local_tech_file_when_available(deck_name):
    path = _tech_file(deck_name)
    if path is None:
        pytest.skip("pinned PDK tech file not installed locally")
    blocks = _contact_blocks(path.read_text())
    for cut in get_parasitics_deck(deck_name).cuts:
        for corner, value in (
            (NOMINAL_CORNER, cut.nominal_ohm),
            (HIGH_CORNER, cut.high_ohm),
            (LOW_CORNER, cut.low_ohm),
        ):
            assert value == pytest.approx(blocks[corner][cut.name] / 1000), (
                deck_name,
                cut.name,
                corner,
            )
