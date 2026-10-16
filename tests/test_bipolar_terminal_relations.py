"""Bipolar terminal relations (issue #2863).

A :class:`~klayout_tools.decks.BipolarDevice` terminal can be defined as the
subset of its layer that interacts with *another terminal of the same
device* (:class:`~klayout_tools.decks.TerminalRelation`), optionally through
that layer's per-polygon extents -- SG13G2's ``pnpMPA`` idiom
``pnp_b.interacting(pnp_b.extents.interacting(pnp_mpa_e))``.

The fixtures are synthetic, built headless with ``klayout.db`` on sky130's
curated layer numbers (nwell 64/20 base, diff 65/20 emitter, pnp.drawing
82/44 marker), with the sky130 deck's ``bipolars`` swapped for a
relation-defined entry via ``monkeypatch`` -- no shipped deck declares a
relation, and ``sg13g2.py`` is deliberately left without bipolars (#2864).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import (
    BipolarDevice,
    TerminalRelation,
    get_extraction_deck,
    history,
)
from klayout_tools.extract import run_extract

NWELL = (64, 20)
DIFF = (65, 20)
PNP_MARK = (82, 44)
LICON = (66, 44)
LI1 = (67, 20)
LI1_LABEL = (67, 5)
NWELL_LABEL = (64, 5)

# Each unit: a 400x400 emitter at the centre of a 2000x2000 nwell ring
# (ring width 300), so the ring never touches its own emitter.
_UNIT = 2000
_RING = 300


def _draw(layout: kdb.Layout, cell: kdb.Cell, pair, shape) -> None:
    cell.shapes(layout.layer(*pair)).insert(shape)


def _label(layout: kdb.Layout, cell: kdb.Cell, pair, text: str, x: int, y: int):
    cell.shapes(layout.layer(*pair)).insert(kdb.Text(text, kdb.Trans(x, y)))


def _ring(x0: int, y0: int, size: int, width: int) -> kdb.Region:
    outer = kdb.Region(kdb.Box(x0, y0, x0 + size, y0 + size))
    inner = kdb.Region(
        kdb.Box(x0 + width, y0 + width, x0 + size - width, y0 + size - width)
    )
    return outer - inner


def _emitter(layout, top, x0: int, y0: int, name: str) -> None:
    """A contacted, labelled 400x400 p+ emitter centred in the unit at x0."""
    cx, cy = x0 + _UNIT // 2, y0 + _UNIT // 2
    _draw(layout, top, DIFF, kdb.Box(cx - 200, cy - 200, cx + 200, cy + 200))
    _draw(layout, top, LICON, kdb.Box(cx - 100, cy - 100, cx + 100, cy + 100))
    _draw(layout, top, LI1, kdb.Box(cx - 150, cy - 150, cx + 150, cy + 150))
    _label(layout, top, LI1_LABEL, name, cx, cy)


def _make_ring_array_layout(
    *, units: int = 2, gap: int = 200, stray_ring: bool = True
) -> kdb.Layout:
    """``units`` pnpMPA-shaped devices side by side under **one** shared
    device marker: each an emitter surrounded (not touched) by its own nwell
    base ring, labelled ``B<i>``/``E<i>``. With ``stray_ring``, a third
    labelled ring ``BX`` with *no* emitter sits just past the last unit,
    inside the same marker -- the negative control a correct relation must
    not capture."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    pitch = _UNIT + gap
    total = units + (1 if stray_ring else 0)
    _draw(layout, top, PNP_MARK, kdb.Box(-500, -500, total * pitch + 500, _UNIT + 500))
    for index in range(units):
        x0 = index * pitch
        for poly in _ring(x0, 0, _UNIT, _RING).each():
            _draw(layout, top, NWELL, poly)
        _label(layout, top, NWELL_LABEL, f"B{index + 1}", x0 + _RING // 2, _UNIT // 2)
        _emitter(layout, top, x0, 0, f"E{index + 1}")
    if stray_ring:
        x0 = units * pitch
        for poly in _ring(x0, 0, _UNIT, _RING).each():
            _draw(layout, top, NWELL, poly)
        _label(layout, top, NWELL_LABEL, "BX", x0 + _RING // 2, _UNIT // 2)
    return layout


def _make_island_array_layout() -> kdb.Layout:
    """Two solid nwell islands each containing a labelled emitter, plus a
    third labelled island ``BX`` with no emitter -- the plain
    (non-``extents``) ``interacting`` case, where the base overlaps its
    emitter."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    pitch = _UNIT + 200
    _draw(layout, top, PNP_MARK, kdb.Box(-500, -500, 3 * pitch + 500, _UNIT + 500))
    for index, name in enumerate(("B1", "B2", "BX")):
        x0 = index * pitch
        _draw(layout, top, NWELL, kdb.Box(x0, 0, x0 + _UNIT, _UNIT))
        _label(layout, top, NWELL_LABEL, name, x0 + 150, 150)
        if name != "BX":
            _emitter(layout, top, x0, 0, f"E{index + 1}")
    return layout


def _write(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _deck_with(bipolar: BipolarDevice):
    def _get(name="sky130", deck_options=None, label_layers=None):
        return dataclasses.replace(
            get_extraction_deck(name, deck_options, label_layers),
            bipolars=(bipolar,),
        )

    return _get


def _pnp(**relations) -> BipolarDevice:
    return BipolarDevice(
        base=NWELL, emitter=DIFF, marker=PNP_MARK, class_name="pnp", **relations
    )


def _extract(tmp_path, monkeypatch, layout, bipolar):
    monkeypatch.setattr(
        "klayout_tools.extract.get_extraction_deck", _deck_with(bipolar)
    )
    path = _write(layout, tmp_path / "bjt.gds")
    return run_extract(path, "sky130", output=str(tmp_path / "bjt.spice"))


def _bjt_nets(report) -> list[dict]:
    return sorted(
        (d["nets"] for d in report["devices"] if d["class"] == "pnp"),
        key=lambda nets: nets["e"],
    )


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_extents_relation_extracts_two_ring_devices_with_distinct_base_nets(
    tmp_path, monkeypatch
):
    """Acceptance (2): a two-device pnpMPA-shaped array with separate base
    rings under one shared marker extracts exactly two devices, each with
    its *own* ring as its base net -- and the emitter-less stray ring ``BX``
    in the same marker (the negative control) is captured by neither."""
    report = _extract(
        tmp_path,
        monkeypatch,
        _make_ring_array_layout(),
        _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
    )

    assert report["device_counts"] == {"pnp": 2}
    assert _bjt_nets(report) == [
        {"c": "vsubs", "b": "B1", "e": "E1"},
        {"c": "vsubs", "b": "B2", "e": "E2"},
    ]


def test_extents_relation_resolves_only_rings_around_an_emitter():
    """The negative control at the region level: of the three marker-scoped
    nwell rings, the resolved base is exactly the two that surround an
    emitter -- the stray ring ``BX`` is not part of it (an unfiltered
    candidate would extract the same two devices, so the device count alone
    cannot prove this)."""
    from klayout_tools.extract import _bipolar_relation_regions

    layout = _make_ring_array_layout()
    top = layout.top_cell()
    bipolar = _pnp(base_relation=TerminalRelation(source="emitter", extents=True))
    base, emitter, collector, dropped = _bipolar_relation_regions(
        layout, top, bipolar, kdb.Region()
    )

    pitch = _UNIT + 200
    expected = _ring(0, 0, _UNIT, _RING) + _ring(pitch, 0, _UNIT, _RING)
    assert (base ^ expected).is_empty()
    assert base.merged().count() == 2
    assert emitter.merged().count() == 2
    assert collector.is_empty()
    assert dropped == 0


def test_relation_path_honours_the_dummy_marker():
    """A dummy marker fully covering the first unit drops it (counted, the
    same way the legacy path counts a consumed base) and leaves the second
    unit's base and emitter intact."""
    from klayout_tools.extract import _bipolar_relation_regions

    layout = _make_ring_array_layout(stray_ring=False)
    bipolar = _pnp(base_relation=TerminalRelation(source="emitter", extents=True))
    dummy = kdb.Region(kdb.Box(-100, -100, _UNIT + 100, _UNIT + 100))
    base, emitter, _collector, dropped = _bipolar_relation_regions(
        layout, layout.top_cell(), bipolar, dummy
    )

    assert dropped == 1
    assert base.merged().count() == 1
    assert emitter.merged().count() == 1
    assert (base & dummy).is_empty()


def test_extents_relation_with_abutting_rings_keeps_bases_apart(tmp_path, monkeypatch):
    """Rings only 20 nm apart (adjacent candidate base geometry) still
    attribute each ring to its own emitter only."""
    report = _extract(
        tmp_path,
        monkeypatch,
        _make_ring_array_layout(gap=20),
        _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
    )

    assert report["device_counts"] == {"pnp": 2}
    assert [nets["b"] for nets in _bjt_nets(report)] == ["B1", "B2"]


def test_same_ring_array_without_a_relation_extracts_no_device(tmp_path, monkeypatch):
    """Control: the legacy base-first path (no relation) cannot express the
    ring geometry -- the emitter is scoped to ``diff & (nwell & marker)``,
    which is empty because no ring covers its emitter -- so the same layout
    extracts no bipolar at all. This is the gap the relation closes."""
    report = _extract(tmp_path, monkeypatch, _make_ring_array_layout(), _pnp())

    assert report["device_counts"].get("pnp", 0) == 0


def test_plain_interacting_relation_skips_the_emitterless_island(tmp_path, monkeypatch):
    """``extents=False``: the base is only the candidate polygons that
    themselves interact with the emitter -- two devices with their own
    island nets, and the emitter-less island ``BX`` is not selected."""
    report = _extract(
        tmp_path,
        monkeypatch,
        _make_island_array_layout(),
        _pnp(base_relation=TerminalRelation(source="emitter")),
    )

    assert report["device_counts"] == {"pnp": 2}
    assert _bjt_nets(report) == [
        {"c": "vsubs", "b": "B1", "e": "E1"},
        {"c": "vsubs", "b": "B2", "e": "E2"},
    ]


def test_plain_interacting_does_not_select_a_ring_around_its_emitter(
    tmp_path, monkeypatch
):
    """``extents=False`` on a ring that surrounds but does not touch its
    emitter selects nothing -- the ``extents`` option is what reaches it."""
    report = _extract(
        tmp_path,
        monkeypatch,
        _make_ring_array_layout(),
        _pnp(base_relation=TerminalRelation(source="emitter")),
    )

    assert report["device_counts"].get("pnp", 0) == 0


def test_relation_with_empty_source_matches_nothing(tmp_path, monkeypatch):
    """No emitter drawn anywhere: the relation selects no base, and the
    layout extracts no bipolar (rather than erroring)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw(layout, top, PNP_MARK, kdb.Box(-500, -500, 2500, 2500))
    for poly in _ring(0, 0, _UNIT, _RING).each():
        _draw(layout, top, NWELL, poly)
    _label(layout, top, NWELL_LABEL, "B1", 150, 1000)

    report = _extract(
        tmp_path,
        monkeypatch,
        layout,
        _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
    )

    assert report["device_counts"].get("pnp", 0) == 0


def test_emitter_relation_against_legacy_base_matches_static_path(
    tmp_path, monkeypatch
):
    """A relation on the emitter while the base stays relation-free keeps
    the base's legacy scoping, so the island layout extracts identically to
    the no-relation entry."""
    with_relation = _extract(
        tmp_path,
        monkeypatch,
        _make_island_array_layout(),
        _pnp(emitter_relation=TerminalRelation(source="base")),
    )
    legacy = _extract(tmp_path, monkeypatch, _make_island_array_layout(), _pnp())

    assert with_relation["device_counts"] == legacy["device_counts"] == {"pnp": 2}
    assert _bjt_nets(with_relation) == _bjt_nets(legacy)


def test_shipped_bipolar_entries_declare_no_relation():
    """Acceptance (3) guard: the shipped curated bipolar entries carry no
    relation, so they keep taking the unchanged legacy path (their own
    extraction tests in ``test_extract.py`` pin the result), and ``sg13g2``
    still declares no bipolar at all (#2864 owns that decision)."""
    for name in ("sky130", "gf180mcu"):
        for bipolar in get_extraction_deck(name).bipolars:
            assert not bipolar.has_relations
    assert get_extraction_deck("sg13g2").bipolars == ()


# --------------------------------------------------------------------------- #
# Validation and resolution order
# --------------------------------------------------------------------------- #


def test_resolution_order_is_deterministic():
    assert _pnp().terminal_resolution_order() == ("base", "emitter")
    assert _pnp(
        base_relation=TerminalRelation(source="emitter", extents=True)
    ).terminal_resolution_order() == ("emitter", "base")
    with_collector = BipolarDevice(
        base=NWELL,
        emitter=DIFF,
        marker=PNP_MARK,
        collector=(65, 44),
        base_relation=TerminalRelation(source="emitter", extents=True),
        collector_relation=TerminalRelation(source="base"),
    )
    assert with_collector.terminal_resolution_order() == (
        "emitter",
        "base",
        "collector",
    )


def test_relation_unknown_source_terminal_is_rejected():
    with pytest.raises(ValueError, match="unknown source terminal 'drain'"):
        TerminalRelation(source="drain")


def test_relation_unknown_op_is_rejected():
    with pytest.raises(ValueError, match="unknown op 'covering'"):
        TerminalRelation(source="emitter", op="covering")


def test_self_reference_is_rejected():
    with pytest.raises(ValueError, match="references base itself"):
        _pnp(base_relation=TerminalRelation(source="base"))


def test_cycle_is_rejected():
    with pytest.raises(ValueError, match="dependency cycle"):
        _pnp(
            base_relation=TerminalRelation(source="emitter"),
            emitter_relation=TerminalRelation(source="base"),
        )


def test_relation_against_absent_collector_is_rejected():
    with pytest.raises(ValueError, match="collector is None"):
        _pnp(base_relation=TerminalRelation(source="collector"))


def test_relation_on_absent_collector_is_rejected():
    with pytest.raises(
        ValueError, match="collector_relation is set but collector is None"
    ):
        _pnp(collector_relation=TerminalRelation(source="emitter"))


def test_relation_distinguishes_otherwise_identical_entries():
    plain = _pnp(base_relation=TerminalRelation(source="emitter"))
    extents = _pnp(base_relation=TerminalRelation(source="emitter", extents=True))
    assert plain != extents
    assert plain != _pnp()


# --------------------------------------------------------------------------- #
# klt deck devices projection
# --------------------------------------------------------------------------- #


def _bipolar_terminals(monkeypatch, bipolar: BipolarDevice) -> dict[str, dict]:
    deck = dataclasses.replace(get_extraction_deck("sky130"), bipolars=(bipolar,))
    monkeypatch.setattr(
        "klayout_tools.decks.get_extraction_deck", lambda *_a, **_k: deck
    )
    report = history.deck_devices("sky130", device_class="pnp")
    (entry,) = report["device_classes"]
    return {terminal["name"]: terminal for terminal in entry["terminals"]}


def test_deck_devices_reports_the_relation(monkeypatch):
    terminals = _bipolar_terminals(
        monkeypatch,
        _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
    )
    assert terminals["base"]["relation"] == {
        "source": "emitter",
        "op": "interacting",
        "extents": True,
    }
    assert terminals["emitter"]["relation"] is None
    assert terminals["collector"]["relation"] is None


def test_deck_devices_distinguishes_relation_variants(monkeypatch):
    plain = _bipolar_terminals(
        monkeypatch, _pnp(base_relation=TerminalRelation(source="emitter"))
    )
    extents = _bipolar_terminals(
        monkeypatch,
        _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
    )
    assert plain["base"] != extents["base"]


def test_deck_devices_reports_null_relation_for_shipped_terminals():
    report = history.deck_devices("gf180mcu")
    for entry in report["device_classes"]:
        for terminal in entry["terminals"]:
            assert terminal["relation"] is None


def test_deck_devices_text_output_names_the_relation(monkeypatch, capsys):
    from klayout_tools.cli import main

    deck = dataclasses.replace(
        get_extraction_deck("sky130"),
        bipolars=(
            _pnp(base_relation=TerminalRelation(source="emitter", extents=True)),
        ),
    )
    monkeypatch.setattr(
        "klayout_tools.decks.get_extraction_deck", lambda *_a, **_k: deck
    )

    assert main(["deck", "devices", "--deck", "sky130", "--class", "pnp"]) == 0
    out = capsys.readouterr().out
    assert "relation: interacting emitter (via extents)" in out
