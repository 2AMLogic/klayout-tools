"""A ``--abstract-cell-lef`` pin is probed on its own LEF ``PORT`` layer first
(issue #2658).

``klt extract --abstract-cells`` resolves an abstracted cell type's pins from
a ``--abstract-cell-lef`` MACRO when the cell draws no in-cell label. Until
this issue each such pin carried **no** layer role, so
``_probe_single_abstract_pin_point`` skipped straight to its bottom-up
cross-layer fallback (``metal0``, ``metal1``, ``metal2``, ... then
``poly``/``nwell``/``tap``). For a foundry hard macro whose ports sit on an
upper metal, placed over a routed parent power grid drawn on a *lower* metal,
that fallback hits the power plane before the port's own metal is ever tried
-- every separately declared pin collapses onto the one parent net, and the
resulting black box makes a downstream ``klt lvs`` compare structurally
meaningless at the macro boundary.

The fix mirrors what issue #2142 already gave in-cell-label pins: each LEF
``PORT`` box's own ``LAYER`` name is translated to a ``deck.metals`` level
through the active PDK's KLayout ``.map`` file, and that level is probed
first. Strictly additive -- with no resolvable map (no ``--pdk``, no map file,
or a LEF layer name the map does not list) the pin keeps today's role-free
bottom-up behaviour, which the collapse regression guard below pins down
explicitly.
"""

import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import run_extract
from klayout_tools.extract_abstract import lef_layer_probe_roles

#: sky130 ``(layer, datatype)`` pairs this fixture draws on. ``metal1`` is
#: `deck.metals[1]` (met1.drawing) and ``metal2`` is `deck.metals[2]`
#: (met2.drawing) -- the macro's ports are on the *upper* of the two, the
#: parent's power plane on the lower, which is what makes the bottom-up
#: fallback wrong here.
_MET1 = (68, 20)
_MET1_PIN = (68, 5)
_MET2 = (69, 20)
_MET2_PIN = (69, 5)

#: One port per declared pin, as macro-local micrometres: the bottom-left
#: corner of a 0.6um x 0.6um met2 pad, pitched 2um apart along the macro.
_PIN_COUNT = 8
_PAD_W_UM = 0.6
_PAD_PITCH_UM = 2.0
_PAD_Y0_UM = 1.0

#: Enough of a real open_pdks KLayout LEF/DEF map file for
#: `_resolve_layer_map`/`_load_gds_to_lef_layer_map` to translate the LEF
#: layer names this fixture's macro declares. Copied in shape (not content)
#: from the real `sky130A.map`, including the `NAME` pseudo-entry the parser
#: must skip and the several-purposes-per-layer convention.
_MAP_FILE_TEXT = """\
# comment line, ignored
li1     LEFPIN,NET,SPNET,PIN,VIA 67  20
NAME    li1/LABEL,li1/LEFPIN     67  5
met1    LEFPIN,NET,SPNET,PIN,VIA 68  20
met1    LEFOBS                   68  4
NAME    met1/LABEL,met1/LEFPIN   68  5
met2    LEFPIN,NET,SPNET,PIN,VIA 69  20
met2    LEFOBS                   69  4
NAME    met2/LABEL,met2/LEFPIN   69  5
"""


def _pad_x0_um(index: int) -> float:
    return 1.0 + index * _PAD_PITCH_UM


def _pin_name(index: int) -> str:
    return f"SIG{index}"


def _hard_macro_layout() -> kdb.Layout:
    """A hard macro whose every pin is a met2 pad, placed over a parent met1
    power plane that covers its whole footprint.

    Each pad is reached by a met2 parent wire carrying its own label, so a
    correct extraction binds pin ``SIG<i>`` to net ``SIG<i>``. Nothing
    connects met1 to met2 anywhere (no via is drawn), so the power plane and
    every signal wire are genuinely distinct nets -- the collapse is purely
    a probe-order artifact, never real connectivity.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    macro = layout.create_cell("mylib__hardmacro")
    top = layout.create_cell("top")

    def um(value: float) -> int:
        return round(value / layout.dbu)

    def draw(cell, layer, box_um):
        x0, y0, x1, y1 = box_um
        cell.shapes(layout.layer(*layer)).insert(
            kdb.Box(um(x0), um(y0), um(x1), um(y1))
        )

    def label(cell, layer, text, x_um, y_um):
        cell.shapes(layout.layer(*layer)).insert(
            kdb.Text(text, kdb.Trans(um(x_um), um(y_um)))
        )

    # The macro's own pads. Deliberately no in-cell label on any layer --
    # that is what forces the `--abstract-cell-lef` pin source.
    for index in range(_PIN_COUNT):
        x0 = _pad_x0_um(index)
        draw(macro, _MET2, (x0, _PAD_Y0_UM, x0 + _PAD_W_UM, _PAD_Y0_UM + _PAD_W_UM))

    top.insert(kdb.CellInstArray(macro.cell_index(), kdb.Trans(0, 0)))

    # The parent's power plane: met1, covering the macro footprint entirely.
    plane_x1 = _pad_x0_um(_PIN_COUNT - 1) + _PAD_W_UM + 1.0
    draw(top, _MET1, (0.0, 0.0, plane_x1, 4.0))
    label(top, _MET1_PIN, "VPWR", plane_x1 / 2, 3.0)

    # One met2 signal wire per pin, landing on that pin's pad and running
    # out of the macro's footprint to its own label.
    for index in range(_PIN_COUNT):
        x0 = _pad_x0_um(index)
        draw(top, _MET2, (x0, _PAD_Y0_UM, x0 + _PAD_W_UM, 8.0))
        label(top, _MET2_PIN, _pin_name(index), x0 + _PAD_W_UM / 2, 7.0)

    return layout


def _write_lef(path: Path) -> str:
    """A LEF MACRO declaring every pad as its own ``PIN``, on ``met2``."""
    lines = ["VERSION 5.7 ;", "MACRO mylib__hardmacro", "  ORIGIN 0.000 0.000 ;"]
    for index in range(_PIN_COUNT):
        x0 = _pad_x0_um(index)
        lines += [
            f"  PIN {_pin_name(index)}",
            "    DIRECTION INOUT ;",
            "    PORT",
            "      LAYER met2 ;",
            f"        RECT {x0:.3f} {_PAD_Y0_UM:.3f} "
            f"{x0 + _PAD_W_UM:.3f} {_PAD_Y0_UM + _PAD_W_UM:.3f} ;",
            "    END",
            f"  END {_pin_name(index)}",
        ]
    lines += ["END mylib__hardmacro", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _make_pdk_install(tmp_path: Path, variant: str, *, with_map: bool = True) -> str:
    """A minimal resolvable PDK install (:func:`klayout_tools.pdk.find_pdk`).

    ``with_map=False`` ships the ``klayout`` asset directory with no ``.map``
    file at all -- the "no layer map resolves" regression case.
    """
    root = tmp_path / f"pdk_install_{variant}_{'map' if with_map else 'nomap'}"
    tech_dir = root / variant / "libs.tech" / "klayout" / "tech"
    tech_dir.mkdir(parents=True)
    if with_map:
        (tech_dir / f"{variant}.map").write_text(_MAP_FILE_TEXT, encoding="utf-8")
    return str(root)


def _extract(tmp_path, *, pdk_root: str | None, tag: str):
    gds = tmp_path / "hardmacro.gds"
    if not gds.exists():
        _hard_macro_layout().write(str(gds))
    lef = tmp_path / "hardmacro.lef"
    if not lef.exists():
        _write_lef(lef)
    return run_extract(
        str(gds),
        "sky130",
        output=str(tmp_path / f"hardmacro-{tag}.spice"),
        abstract_cell_patterns=("mylib__*",),
        abstract_cell_lef_paths=(str(lef),),
        pdk_variant="sky130A" if pdk_root is not None else None,
        pdk_root=pdk_root,
    )


def _instance_pin_nets(extraction, cell: str, instance: str) -> dict[str, str]:
    """``{<pin name>: <parent net the X card binds it to>}``.

    Read off the written netlist rather than the JSON so the assertion is
    made against the artifact a downstream ``klt lvs`` compare consumes.
    """
    text = Path(extraction["netlist_path"]).read_text()
    subckt = re.search(rf"(?m)^\.SUBCKT {re.escape(cell)}((?: \S+)*)\s*$", text)
    assert subckt is not None, f"no .SUBCKT {cell} in:\n{text}"
    pin_names = subckt.group(1).split()
    card = re.search(
        rf"(?m)^X{re.escape(instance)}((?: \S+)+) {re.escape(cell)}\s*$", text
    )
    assert card is not None, f"no X{instance} card in:\n{text}"
    nodes = card.group(1).split()
    assert len(nodes) == len(pin_names), f"arity mismatch in:\n{text}"
    return dict(zip(pin_names, nodes, strict=True))


def test_layer_role_lookup_covers_the_deck_stack_and_degrades_quietly(tmp_path):
    """`lef_layer_probe_roles` turns the map file's LEF layer names into the
    `probe_layers` role strings `extract.py` builds over `deck.metals`, and
    returns an empty dict (today's role-free behaviour) for every way the
    lookup can come up short."""
    deck = get_extraction_deck("sky130")
    root = Path(_make_pdk_install(tmp_path, "sky130A"))
    pdk_info = {
        "variant": "sky130A",
        "assets": {"klayout": str(root / "sky130A" / "libs.tech" / "klayout")},
    }

    roles = lef_layer_probe_roles(deck, pdk_info)
    # Only the three levels this fixture's map file names -- met3/met4/met5
    # are real deck metals the map deliberately omits, and must simply be
    # absent rather than mapped to something else.
    assert roles == {"li1": "metal0", "met1": "metal1", "met2": "metal2"}

    # Case-insensitive on the LEF side (a LEF may spell `MET2`), and a LEF
    # layer the map never names resolves to nothing at all.
    assert roles.get("met4") is None
    assert roles.get("mcon") is None

    assert lef_layer_probe_roles(deck, None) == {}
    assert (
        lef_layer_probe_roles(
            deck,
            {
                "variant": "sky130A",
                "assets": {
                    "klayout": str(
                        Path(_make_pdk_install(tmp_path, "sky130A", with_map=False))
                        / "sky130A"
                        / "libs.tech"
                        / "klayout"
                    )
                },
            },
        )
        == {}
    )
    assert lef_layer_probe_roles(deck, {"variant": "sky130A", "assets": {}}) == {}


def test_lef_port_layer_resolves_each_pin_to_its_own_met2_net(tmp_path):
    """Issue #2658: with the PDK's KLayout `.map` resolvable, every met2 LEF
    port is probed on met2 first, so each declared pin binds to its own
    signal net instead of collapsing onto the met1 power plane underneath."""
    report = _extract(
        tmp_path, pdk_root=_make_pdk_install(tmp_path, "sky130A"), tag="mapped"
    )

    (entry,) = report["abstracted_cells"]
    assert entry["resolution_source"] == "lef_abstract"
    assert entry["pin_count"] == _PIN_COUNT

    bound = _instance_pin_nets(report, "mylib__hardmacro", "mylib__hardmacro_0")
    assert bound == {_pin_name(index): _pin_name(index) for index in range(_PIN_COUNT)}

    # ...and the many-pins-one-net warning that the collapse used to be
    # disclosed by is gone, because there is no collapse left to report.
    assert not [
        warning
        for warning in report["warnings"]
        if "separately declared pins onto the same net" in warning
    ], report["warnings"]


def test_tied_pin_warning_still_fires_for_genuinely_tied_lef_pins(tmp_path):
    """The layer-aware probe must not silence the legal case: two LEF pins
    whose met2 ports land on one drawn met2 wire are still reported as tied
    (issue #1366's warning, unchanged)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    macro = layout.create_cell("mylib__tied")
    top = layout.create_cell("top")

    def um(value: float) -> int:
        return round(value / layout.dbu)

    def box(x0, y0, x1, y1):
        return kdb.Box(um(x0), um(y0), um(x1), um(y1))

    # Two pads, and a single met2 wire in the macro shorting them together.
    macro.shapes(layout.layer(*_MET2)).insert(box(1.0, 1.0, 4.0, 1.6))
    top.insert(kdb.CellInstArray(macro.cell_index(), kdb.Trans(0, 0)))
    top.shapes(layout.layer(*_MET2)).insert(box(1.0, 1.0, 1.6, 6.0))
    top.shapes(layout.layer(*_MET2_PIN)).insert(
        kdb.Text("NETA", kdb.Trans(um(1.3), um(5.0)))
    )

    gds = tmp_path / "tied.gds"
    layout.write(str(gds))
    lef = tmp_path / "tied.lef"
    lef.write_text(
        "VERSION 5.7 ;\n"
        "MACRO mylib__tied\n"
        "  ORIGIN 0.000 0.000 ;\n"
        "  PIN P0\n"
        "    DIRECTION INOUT ;\n"
        "    PORT\n"
        "      LAYER met2 ;\n"
        "        RECT 1.000 1.000 1.600 1.600 ;\n"
        "    END\n"
        "  END P0\n"
        "  PIN P1\n"
        "    DIRECTION INOUT ;\n"
        "    PORT\n"
        "      LAYER met2 ;\n"
        "        RECT 3.400 1.000 4.000 1.600 ;\n"
        "    END\n"
        "  END P1\n"
        "END mylib__tied\n",
        encoding="utf-8",
    )

    report = run_extract(
        str(gds),
        "sky130",
        output=str(tmp_path / "tied.spice"),
        abstract_cell_patterns=("mylib__*",),
        abstract_cell_lef_paths=(str(lef),),
        pdk_variant="sky130A",
        pdk_root=_make_pdk_install(tmp_path, "sky130A"),
    )

    bound = _instance_pin_nets(report, "mylib__tied", "mylib__tied_0")
    assert bound == {"P0": "NETA", "P1": "NETA"}
    assert [
        warning
        for warning in report["warnings"]
        if "separately declared pins onto the same net" in warning
    ], report["warnings"]


@pytest.mark.parametrize(
    "pdk",
    [
        pytest.param("none", id="no-pdk-flag"),
        pytest.param("no-map", id="pdk-without-map-file"),
    ],
)
def test_without_a_resolvable_layer_map_behaviour_is_unchanged(tmp_path, pdk):
    """Strictly additive: with no `--pdk` at all, or a PDK whose KLayout
    asset ships no `.map` file, the LEF pin keeps its role-free bottom-up
    probe -- which on this fixture is exactly the reported collapse. Pinning
    it down here is what makes the fix above provably the layer role's
    doing, and guards the "unchanged for PDKs without a map file"
    requirement."""
    pdk_root = (
        None
        if pdk == "none"
        else _make_pdk_install(tmp_path, "sky130A", with_map=False)
    )
    report = _extract(tmp_path, pdk_root=pdk_root, tag=pdk)

    bound = _instance_pin_nets(report, "mylib__hardmacro", "mylib__hardmacro_0")
    assert set(bound.values()) == {"VPWR"}, bound
    assert [
        warning
        for warning in report["warnings"]
        if "separately declared pins onto the same net" in warning
    ], report["warnings"]
