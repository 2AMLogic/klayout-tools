"""Issue #5442: sg13g2 `NW.c` does not measure NWell enclosure of P+ Activ
belonging to a text-recognised `SVaricap` varactor, and neither does the
curated `nwell.enclosing.pactiv.1`.

#5212 documented the deviation as a `runset_note`; the markers still
counted, so a stream instantiating the PDK's own varactor could never reach
`status: clean`. These tests pin the modelled carve-out
(`decks/sg13g2_svaricap.py` + `drc_text_exempt.py`): the recogniser is
geometry plus the `63/0` text label, exactly as `sg13g2_maximal.drc`
derives `SVaricap`, and it exempts *whole* P+ Activ polygons -- never a
cell, never a stream.

Every stream here is built in-test. The same code path was additionally run
against the PDK's own `sg13_hv_svaricap` test layout (see the PR): 64
`nwell.enclosing.pactiv.1` findings before, 0 after.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb

from klayout_tools.decks import get_deck
from klayout_tools.decks.sg13g2_svaricap import SVARICAP
from klayout_tools.drc import list_deck_rules, run_drc
from klayout_tools.drc_text_exempt import TextMarkerDeviceSpec

_DBU_UM = 0.001
_RULE = "nwell.enclosing.pactiv.1"
_HV_RULE = "nwell.enclosing.pactiv.hv.1"

# Layers the recogniser reads (IHP sg13g2 drawn layers).
_ACTIV = (1, 0)
_GATPOLY = (5, 0)
_PSD = (14, 0)
_NWELL = (31, 0)
_NBULAY = (32, 0)
_THICKGATEOX = (44, 0)
_TEXT = (63, 0)


def _box_um(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(
        round(x0 / _DBU_UM),
        round(y0 / _DBU_UM),
        round(x1 / _DBU_UM),
        round(y1 / _DBU_UM),
    )


class _Stream:
    """A one-cell sg13g2 layout under construction."""

    def __init__(self, cell_name: str = "TOP") -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = _DBU_UM
        self.cell = self.layout.create_cell(cell_name)

    def box(self, layer: tuple[int, int], *coords: float) -> _Stream:
        self.cell.shapes(self.layout.layer(*layer)).insert(_box_um(*coords))
        return self

    def text(self, label: str, x: float, y: float) -> _Stream:
        trans = kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM))
        self.cell.shapes(self.layout.layer(*_TEXT)).insert(kdb.Text(label, trans))
        return self

    def write(self, tmp_path: Path, name: str) -> str:
        path = str(tmp_path / name)
        self.layout.write(path)
        return path


def _svaricap_stack(
    stream: _Stream,
    x0: float,
    *,
    label: str | None = "SVaricap",
    buried_layer: bool = True,
    gate: bool = True,
) -> _Stream:
    """A recognised `SVaricap` at `x0`: an NWell over nBuLay holding an N+
    Activ plate (no drawn nSD -- N+ is this PDK's default implant) crossed by
    a GatPoly gate, with an "SVaricap" label on the plate.

    `label=None` / `buried_layer=False` / `gate=False` each break exactly one
    leg of the recognition, leaving the geometry otherwise identical.
    """
    stream.box(_NWELL, x0, 0, x0 + 10, 10)
    if buried_layer:
        stream.box(_NBULAY, x0 - 1, -1, x0 + 11, 11)
    stream.box(_ACTIV, x0 + 2, 4, x0 + 8, 6)
    if gate:
        stream.box(_GATPOLY, x0 + 4, 3, x0 + 5, 7)
    if label is not None:
        stream.text(label, x0 + 5, 5)
    return stream


def _under_enclosed_pactiv(stream: _Stream, x0: float) -> _Stream:
    """A P+ Activ whose left edge sits 0.1 um inside the NWell edge at `x0`
    -- below NW.c's 0.31 um, so `nwell.enclosing.pactiv.1` fires on it."""
    stream.box(_ACTIV, x0 + 0.1, 1, x0 + 3, 2)
    stream.box(_PSD, x0 + 0.1, 1, x0 + 3, 2)
    return stream


def _counts(path: str) -> dict[str, int]:
    return run_drc(path, "sg13g2")["rule_counts"]


# --------------------------------------------------------------------------- #
# The three streams issue #5442's acceptance criteria name
# --------------------------------------------------------------------------- #


def test_recognised_svaricap_pactiv_is_not_reported(tmp_path: Path):
    """A P+ Activ under-enclosed by 0.21 um inside a recognised SVaricap's
    NWell produces no `nwell.enclosing.pactiv.1` finding, and the stream is
    clean -- the verdict the foundry runset reaches on the same geometry."""
    stream = _under_enclosed_pactiv(_svaricap_stack(_Stream(), 0), 0)
    report = run_drc(stream.write(tmp_path, "svaricap.gds"), "sg13g2")
    assert report["rule_counts"].get(_RULE, 0) == 0
    assert report["status"] == "clean"
    # The rule still ran: exemption is not a skip (`coverage.rules_checked`).
    assert _RULE in report["coverage"]["rules_checked"]
    assert _RULE not in report["coverage"]["rules_skipped"]


def test_same_geometry_without_the_label_is_reported(tmp_path: Path):
    """Identical geometry, no text label (and with a *different* label):
    today's behaviour, one finding each."""
    for name, label in (("no_label.gds", None), ("other_label.gds", "MVaricap")):
        stream = _under_enclosed_pactiv(_svaricap_stack(_Stream(), 0, label=label), 0)
        assert _counts(stream.write(tmp_path, name)).get(_RULE, 0) == 1, name


def test_exemption_is_per_polygon_not_per_stream(tmp_path: Path):
    """One stream, two wells: a recognised SVaricap at x=0 and an unrelated
    under-enclosed P+ Activ in a plain NWell at x=20. Only the unrelated
    shape is reported."""
    stream = _under_enclosed_pactiv(_svaricap_stack(_Stream(), 0), 0)
    stream.box(_NWELL, 20, 0, 30, 10)
    _under_enclosed_pactiv(stream, 20)

    report = run_drc(stream.write(tmp_path, "mixed.gds"), "sg13g2")
    assert report["rule_counts"].get(_RULE, 0) == 1
    violation = next(v for v in report["violations"] if v["rule"] == _RULE)
    # The reported shape is the one in the *plain* well (x >= 20 um).
    assert violation["bbox"]["left"] >= round(20 / _DBU_UM)


# --------------------------------------------------------------------------- #
# Recognition is geometry + label only, and fails closed
# --------------------------------------------------------------------------- #


def test_cell_name_alone_never_exempts(tmp_path: Path):
    """The geometry sits in a cell literally named `SVaricap`, with no
    label: still reported. Recognition never reads a cell name."""
    stream = _under_enclosed_pactiv(
        _svaricap_stack(_Stream("SVaricap"), 0, label=None), 0
    )
    assert _counts(stream.write(tmp_path, "cellname.gds")).get(_RULE, 0) == 1


def test_incomplete_stack_never_exempts(tmp_path: Path):
    """The label is present but a leg of the stack is missing -- no nBuLay,
    or no gate. Neither exempts anything."""
    cases = {"no_nbulay.gds": {"buried_layer": False}, "no_gate.gds": {"gate": False}}
    for name, kwargs in cases.items():
        stream = _under_enclosed_pactiv(_svaricap_stack(_Stream(), 0, **kwargs), 0)
        assert _counts(stream.write(tmp_path, name)).get(_RULE, 0) == 1, name


def test_absent_text_layer_leaves_the_rule_unchanged(tmp_path: Path):
    """No `63/0` in the stream at all: the rule is checked exactly as before
    this carve-out existed, never skipped."""
    stream = _Stream().box(_NWELL, 0, 0, 10, 10)
    _under_enclosed_pactiv(stream, 0)
    report = run_drc(stream.write(tmp_path, "no_text.gds"), "sg13g2")
    assert report["rule_counts"].get(_RULE, 0) == 1
    assert _RULE in report["coverage"]["rules_checked"]


def test_hv_sibling_rule_is_not_exempted(tmp_path: Path):
    """The carve-out is scoped to NW.c. The same recognised SVaricap with its
    P+ Activ inside ThickGateOx is measured by `nwell.enclosing.pactiv.hv.1`
    (NW.c1, 0.62 um), which this issue leaves unmodelled -- still reported."""
    stream = _under_enclosed_pactiv(_svaricap_stack(_Stream(), 0), 0)
    stream.box(_THICKGATEOX, 0, 0.5, 4, 2.5)
    counts = _counts(stream.write(tmp_path, "hv.gds"))
    assert counts.get(_RULE, 0) == 0
    assert counts.get(_HV_RULE, 0) == 1


# --------------------------------------------------------------------------- #
# Deck data: what the recogniser is, and who carries it
# --------------------------------------------------------------------------- #


def test_only_the_nw_c_rule_of_only_the_sg13g2_deck_carries_a_spec():
    """One rule in one deck. sg13cmos5l's same-named rule is untouched."""
    for deck_name in ("sg13g2", "sg13cmos5l", "sky130", "gf180mcu"):
        deck = get_deck(deck_name)
        carriers = [rule.id for rule in deck if rule.text_exempt is not None]
        assert carriers == ([_RULE] if deck_name == "sg13g2" else []), deck_name

    cmos5l = next(r for r in get_deck("sg13cmos5l") if r.id == _RULE)
    assert cmos5l.text_exempt is None
    assert "SVaricap" not in cmos5l.description


def test_spec_transcribes_the_runset_layers():
    """The deck data is the IHP drawn layers plus the label string -- the
    operands `sg13g2_maximal.drc` builds `SVaricap` from."""
    assert SVARICAP == TextMarkerDeviceSpec(
        activ=_ACTIV,
        gate_poly=_GATPOLY,
        n_implant=(7, 0),
        n_implant_block=(7, 21),
        p_implant=_PSD,
        well=_NWELL,
        buried_layer=_NBULAY,
        text_layer=_TEXT,
        label="SVaricap",
    )
    assert _NBULAY in SVARICAP.layers() and _TEXT in SVARICAP.layers()


def test_listing_and_coverage_disclose_the_carve_out():
    """`klt drc --list-rules` states the exemption in the rule's description,
    and `coverage.deck_layers` names the layers the recogniser reads."""
    listing = list_deck_rules("sg13g2")
    rule = next(r for r in listing["rules"] if r["id"] == _RULE)
    assert "SVaricap" in rule["description"] and "NW.c" in rule["description"]

    sibling = next(r for r in listing["rules"] if r["id"] == _HV_RULE)
    assert "SVaricap" not in sibling["description"]
