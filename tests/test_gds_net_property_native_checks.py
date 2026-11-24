"""DEF net-name shape properties vs. native ``klayout.db`` rule rebuilds (#7715).

The DEF->GDS merge writes each routed wire's DEF net name as a GDS shape
property (``net_property_name``, default ``1``; ``docs/cli/place-and-route.md``).
Standard-cell and via-cell shapes carry none. Merged semantics only merges
shapes with *equal* properties, so a wire that ends inside a via pad stays a
separate polygon -- but only for a ``Region`` that actually sees the
properties.

This file pins, with a minimal layout and no design, exactly when that
matters. The layout is the packaged ASAP7 deck's
``two_side_enclosure_violation(v4, m4, 11.nm)`` situation:

* a 24 x 24 nm V4 cut at the origin;
* an M4 pad 11 nm wider than the cut on the left and right, 0 nm above and
  below (``(-11, 0, 35, 24)``);
* an M4 wire over the cut that ends at the cut's right edge, inside the pad
  (``(-100, 0, 24, 24)``). The wire must extend *over the cut* so that its
  end edge coincides with the cut's right edge; a wire running away to the
  right of the cut (``(24, 0, 200, 24)``) gives a different, non-violating
  edge configuration.

The native rebuild is the deck's own formula over ``Edges.enclosing_check``
(``vert(metal).enclosing(vert(via), d, projection)`` and the ``horz`` twin,
``via.interacting(...second_edges)``, AND-ed). **What reproduces the issue's
table is the Region construction**:

* ``kdb.Region(cell.shapes(layer_index))`` keeps the shape properties, so a
  tagged wire stays its own polygon (merged M4 count 2, one violation);
* ``kdb.Region(cell.begin_shapes_rec(layer_index))`` -- the flattening idiom
  ``klt drc``'s native engine uses (``drc._cell_region_or_empty``) -- ignores
  the properties by default (merged M4 count 1, no violation). Enabling
  properties on the iterator includes the tag when the reader retained it.

Measured with the klayout module 0.30.13 (this venv) and 0.30.14 (the
reporter's version); only the ``enclosing_check`` rebuild is exercised here
(the issue's ``grid_check`` observation is not re-measured).
"""

from __future__ import annotations

import pytest

kdb = pytest.importorskip("klayout.db")

import klayout_tools.drc as drc  # noqa: E402
from klayout_tools.decks import DrcRule  # noqa: E402

V4 = (45, 0)
M4 = (40, 0)

#: The deck's enclosure distance, in dbu (layout dbu is 0.001 um = 1 nm).
ENCLOSURE_DBU = 11


def _write_layout(path, *, tagged_wire: bool) -> str:
    """Write the minimal layout; ``tagged_wire`` carries a net-name property.

    The property mirrors the DEF->GDS merge's: attribute ``1`` (the default
    ``net_property_name``) holding the DEF net name.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(*V4)).insert(kdb.Box(0, 0, 24, 24))
    top.shapes(layout.layer(*M4)).insert(kdb.Box(-11, 0, 35, 24))
    wire = kdb.Box(-100, 0, 24, 24)
    if tagged_wire:
        top.shapes(layout.layer(*M4)).insert(wire, layout.properties_id({1: "net_a"}))
    else:
        top.shapes(layout.layer(*M4)).insert(wire)
    layout.write(str(path))
    return str(path)


def _read(path: str, *, properties_enabled: bool):
    """Read ``path`` back. Returns the layout too: a ``Cell`` does not keep it alive."""
    options = kdb.LoadLayoutOptions()
    options.properties_enabled = properties_enabled
    layout = kdb.Layout()
    layout.read(path, options)
    return layout, layout.top_cell()


def _region(layout, cell, layer: tuple[int, int], construction: str):
    index = layout.layer(*layer)
    if construction == "shapes":
        return kdb.Region(cell.shapes(index))
    iterator = cell.begin_shapes_rec(index)
    if construction == "recursive_with_properties":
        iterator.enable_properties()
    else:
        assert construction == "recursive"
    return kdb.Region(iterator)


def _native_two_side_enclosure(layout, cell, construction: str) -> tuple[int, int]:
    """Native rebuild of ``two_side_enclosure_violation(v4, m4, 11.nm)``.

    Returns ``(violating via cuts, merged M4 polygons)``.
    """
    v4 = _region(layout, cell, V4, construction)
    m4 = _region(layout, cell, M4, construction)

    def vert(region):
        return region.edges().with_angle(90, False)

    def horz(region):
        return region.edges().with_angle(0, False)

    projection = kdb.Region.Projection
    short_h = v4.interacting(
        vert(m4)
        .enclosing_check(vert(v4), ENCLOSURE_DBU, metrics=projection)
        .second_edges()
    )
    short_v = v4.interacting(
        horz(m4)
        .enclosing_check(horz(v4), ENCLOSURE_DBU, metrics=projection)
        .second_edges()
    )
    return (short_h & short_v).count(), m4.merged().count()


# (tagged wire, read with properties) -> (native violations, merged M4 polygons):
# the issue's table, for ``Region(cell.shapes(layer))``.
SHAPES_TABLE = [
    pytest.param(False, True, (0, 1), id="no-property-read-with-properties"),
    pytest.param(False, False, (0, 1), id="no-property-read-without-properties"),
    pytest.param(True, True, (1, 2), id="property-read-with-properties"),
    pytest.param(True, False, (0, 1), id="property-read-without-properties"),
]


@pytest.mark.parametrize(("tagged", "properties_enabled", "expected"), SHAPES_TABLE)
def test_shapes_region_table(tmp_path, tagged, properties_enabled, expected):
    """``Region(cell.shapes(layer))``: only a tagged, property-read wire differs.

    That row is the issue's finding: one violation and two merged M4 polygons.
    """
    path = _write_layout(tmp_path / "m4.gds", tagged_wire=tagged)
    layout, cell = _read(path, properties_enabled=properties_enabled)

    assert _native_two_side_enclosure(layout, cell, "shapes") == expected


@pytest.mark.parametrize(
    ("tagged", "properties_enabled"), [(False, True), (True, True), (True, False)]
)
def test_recursive_region_ignores_properties_by_default(
    tmp_path, tagged, properties_enabled
):
    """``Region(cell.begin_shapes_rec(layer))`` ignores the properties by default.

    Control for the table above, and the construction ``klt drc``'s native
    engine uses: one merged M4 polygon and no violation in every row, tagged
    or not, read with properties or not. (Reading with
    ``properties_enabled = False`` is a no-op for it, which is why the
    guidance is conditional on the construction.)
    """
    path = _write_layout(tmp_path / "m4.gds", tagged_wire=tagged)
    layout, cell = _read(path, properties_enabled=properties_enabled)

    assert _native_two_side_enclosure(layout, cell, "recursive") == (0, 1)


@pytest.mark.parametrize(("tagged", "properties_enabled", "expected"), SHAPES_TABLE)
def test_recursive_iterator_can_include_properties(
    tmp_path, tagged, properties_enabled, expected
):
    path = _write_layout(tmp_path / "m4.gds", tagged_wire=tagged)
    layout, cell = _read(path, properties_enabled=properties_enabled)

    assert (
        _native_two_side_enclosure(layout, cell, "recursive_with_properties")
        == expected
    )


def test_klt_drc_native_engine_report_is_unchanged_by_the_wire_property(
    tmp_path, monkeypatch
):
    """``klt drc``'s own engine reports the same markers with and without the tag.

    ``run_drc`` reads with ``load_layout`` (properties enabled) and builds its
    Regions with ``begin_shapes_rec``, so the tagged and untagged files give
    identical findings. The synthetic rule is a plain M4-encloses-V4 check at
    the deck's 11 nm; the top/bottom 0 nm enclosure guarantees a non-empty
    report, so the equality is not vacuous. If this ever fails, ``klt drc`` is
    property-sensitive: file a separate issue -- do not drop properties in
    ``load_layout``, ``klt extract --def-net-names`` needs them.
    """
    rule = DrcRule(
        id="test.m4.encl.v4",
        description="synthetic 11 nm M4 enclosure of V4",
        check="enclosing",
        layer=M4,
        other_layer=V4,
        threshold_dbu=ENCLOSURE_DBU,
    )
    monkeypatch.setattr(drc, "get_deck", lambda _: [rule])

    plain = drc.run_drc(
        _write_layout(tmp_path / "plain.gds", tagged_wire=False), "sky130"
    )
    tagged = drc.run_drc(
        _write_layout(tmp_path / "tagged.gds", tagged_wire=True), "sky130"
    )

    assert plain["violation_count"] > 0
    assert tagged["rule_counts"] == plain["rule_counts"]
    assert tagged["violations"] == plain["violations"]


def _write_two_wells(path, wire: str | None) -> str:
    """Two sg13cmos5l NWells 1.0 um apart, each tied by an n+ tap -> Cont -> Metal1 pad.

    ``wire`` is ``None`` (no bridge), ``"plain"`` or ``"tagged"``: a Metal1
    wire that ends inside both pads, joining the two taps into one net.
    """
    nwell, activ, nsd, cont, metal1 = (31, 0), (1, 0), (7, 0), (6, 0), (8, 0)
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    def shapes(pair):
        return top.shapes(layout.layer(*pair))

    for x0 in (0, 3000):
        shapes(nwell).insert(kdb.Box(x0, 0, x0 + 2000, 2000))
        shapes(activ).insert(kdb.Box(x0 + 500, 500, x0 + 900, 900))
        shapes(nsd).insert(kdb.Box(x0 + 400, 400, x0 + 1000, 1000))
        shapes(cont).insert(kdb.Box(x0 + 600, 600, x0 + 800, 800))
        shapes(metal1).insert(kdb.Box(x0 + 500, 500, x0 + 900, 900))
    bridge = kdb.Box(700, 600, 3700, 800)
    if wire == "plain":
        shapes(metal1).insert(bridge)
    elif wire == "tagged":
        shapes(metal1).insert(bridge, layout.properties_id({1: "net_a"}))
    layout.write(str(path))
    return str(path)


def test_klt_drc_net_space_rule_is_unchanged_by_the_wire_property(tmp_path):
    """The one ``LayoutToNetlist`` rule (``net_space``) is not property-sensitive.

    ``nwell.space.2`` flags two wells <1.8 um apart only when they sit on
    different nets. With no wire they do (the control: the rule is live on
    this geometry); a Metal1 wire joining the two taps makes it one net --
    identically whether or not that wire carries a net-name property.
    """

    def run(name, wire):
        return drc.run_drc(
            _write_two_wells(tmp_path / f"{name}.gds", wire),
            "sg13cmos5l",
            rules=["nwell.space.2"],
        )

    unbridged = run("none", None)
    plain = run("plain", "plain")
    tagged = run("tagged", "tagged")

    assert unbridged["rule_counts"] == {"nwell.space.2": 2}
    assert plain["violations"] == [] and plain["rule_counts"] == {}
    assert tagged["violations"] == plain["violations"]
    assert tagged["rule_counts"] == plain["rule_counts"]
