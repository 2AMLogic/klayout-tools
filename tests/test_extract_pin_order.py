"""``--pins`` order defines the top ``.SUBCKT`` pin order (issue #5255)."""

from __future__ import annotations

import pytest

from klayout_tools.cli import main
from klayout_tools.cli._parsing import parse_declared_pins
from klayout_tools.extract_pin_order import _apply_pin_order_to_spice
from test_extract import _make_inverter_layout, _write_gds


def _write(tmp_path, header: str) -> str:
    path = tmp_path / "x.spice"
    path.write_text(f"* c\n{header}\nR1 a b 1k\n.ENDS top\n")
    return str(path)


def test_parse_declared_pins_keeps_order_and_set_semantics():
    pins = parse_declared_pins("en, out,vdd,vss,en", ValueError)
    assert pins == frozenset({"en", "out", "vdd", "vss"})
    assert pins.order == ("en", "out", "vdd", "vss")


def test_declared_pins_first_in_given_order(tmp_path):
    path = _write(tmp_path, ".SUBCKT top out en vss vdd")
    _apply_pin_order_to_spice(path, "top", ("en", "out", "vdd", "vss"))
    assert ".SUBCKT top en out vdd vss\n" in open(path).read()


def test_unlisted_pins_follow_in_existing_order_and_unknown_ignored(tmp_path):
    path = _write(tmp_path, ".SUBCKT top a b c d")
    _apply_pin_order_to_spice(path, "top", ("D", "nomatch", "b"))
    assert ".SUBCKT top d b a c\n" in open(path).read()


def test_empty_order_is_noop(tmp_path):
    path = _write(tmp_path, ".SUBCKT top out en")
    before = open(path).read()
    _apply_pin_order_to_spice(path, "top", ())
    assert open(path).read() == before


def test_continuation_lines_are_rejoined(tmp_path):
    names = [f"pin{i:02d}_long_name" for i in range(12)]
    header = ".SUBCKT top " + " ".join(names[:5]) + "\n+ " + " ".join(names[5:])
    path = _write(tmp_path, header)
    _apply_pin_order_to_spice(path, "top", (names[-1],))
    text = open(path).read()
    flat = " ".join(
        line.lstrip("+ ")
        for line in text.splitlines()
        if line.startswith(("+", ".SUBCKT"))
    ).split()
    assert flat[2:] == [names[-1]] + names[:-1]
    assert "R1 a b 1k" in text


def test_cli_pins_flag_order_defines_subckt_pin_order(tmp_path, capsys):
    """`--pins` order is the emitted top `.SUBCKT` pin order (issue #5255)."""
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    out_path = tmp_path / "inv.spice"
    exit_code = main(
        [
            "extract", path, "--deck", "sky130", "-o", str(out_path),
            "--pins", "Y,VPWR,VGND,VPB", "--format", "json",
        ]
    )  # fmt: skip
    assert exit_code == 0
    capsys.readouterr()
    header = next(
        line for line in out_path.read_text().splitlines()
        if line.lower().startswith(".subckt")
    )  # fmt: skip
    pins = header.split()[2:]
    assert pins[:4] == ["Y", "VPWR", "VGND", "VPB"]


def test_joined_label_pin_hoists_on_component_match(tmp_path):
    """Issue #5534: a declared name hoists a pin whose *component* labels
    contain it, not only a whole-token match (`--pins`' own #1687 rule)."""
    path = _write(tmp_path, ".SUBCKT top a|a0 b c|x d")
    _apply_pin_order_to_spice(path, "top", ("d", "x", "a"))
    assert ".SUBCKT top d c|x a|a0 b\n" in open(path).read()


def test_pin_matching_two_declared_names_is_emitted_once_at_earliest(tmp_path):
    """Issue #5534: a joined-label pin matching several declared names appears
    exactly once, at the position of the earliest name it matches."""
    path = _write(tmp_path, ".SUBCKT top z a|b y")
    _apply_pin_order_to_spice(path, "top", ("a", "b"))
    assert ".SUBCKT top a|b z y\n" in open(path).read()


def test_whole_token_pipe_declared_name_hoists(tmp_path):
    """A header token already present may also be matched by its full spelling."""
    path = _write(tmp_path, ".SUBCKT top z a|b y")
    _apply_pin_order_to_spice(path, "top", ("a|b",))
    assert ".SUBCKT top a|b z y\n" in open(path).read()


def test_literal_label_wins_over_another_ports_joined_spelling(tmp_path):
    path = _write(tmp_path, r".SUBCKT top gnd Y|ybar Y\x7cybar vpwr")
    _apply_pin_order_to_spice(path, "top", ("gnd", "Y|ybar", "vpwr", "ybar"))
    assert r".SUBCKT top gnd Y\x7cybar vpwr Y|ybar" in open(path).read()


def test_whole_token_match_stays_case_insensitive(tmp_path):
    """Issue #6021: the whole-token fallback keeps #5255's
    case-insensitivity, like the component match."""
    path = _write(tmp_path, ".SUBCKT top z a|b y")
    _apply_pin_order_to_spice(path, "top", ("A|B",))
    assert ".SUBCKT top a|b z y\n" in open(path).read()


def test_component_label_match_is_case_insensitive(tmp_path):
    """Issue #5534: component matching keeps #5255's case-insensitivity."""
    path = _write(tmp_path, ".SUBCKT top q VGND|vss")
    _apply_pin_order_to_spice(path, "top", ("VSS",))
    assert ".SUBCKT top VGND|vss q\n" in open(path).read()


def test_cli_pins_order_holds_for_joined_label_nets(tmp_path, capsys):
    """Issue #5534: end-to-end, a layout carrying both a single-label net and
    a joined-label net emits the top `.SUBCKT` header in declared order."""
    layout = _make_inverter_layout(extra_y_label="ybar")
    path = str(_write_gds(layout, tmp_path / "inv.gds"))
    out_path = tmp_path / "inv.spice"
    exit_code = main(
        [
            "extract", path, "--deck", "sky130", "-o", str(out_path),
            "--pins", "VPWR,ybar,VGND", "--format", "json",
        ]
    )  # fmt: skip
    assert exit_code == 0
    capsys.readouterr()
    header = next(
        line for line in out_path.read_text().splitlines()
        if line.lower().startswith(".subckt")
    )  # fmt: skip
    # `Y|ybar` matches declared `ybar` through its component label, so it is
    # hoisted into the declared slot rather than trailing the whole-name hits.
    assert header.split()[2:] == ["VPWR", "Y|ybar", "VGND"]


@pytest.mark.parametrize(
    ("literal", "declared", "expected"),
    [
        (True, "Y|ybar", ["VGND", r"Y\x7cybar", "VPWR"]),
        (False, "Y|ybar", ["VGND", "VPWR"]),
        (False, "ybar", ["VGND", "Y|ybar", "VPWR"]),
    ],
    ids=["literal-pipe", "two-labels-joined-spelling", "two-labels-component"],
)
def test_cli_pipe_label_promotion_and_order(
    tmp_path, capsys, literal, declared, expected
):
    layout = _make_inverter_layout(extra_y_label=None if literal else "ybar")
    if literal:
        # Replace the existing label on the PMOS drain with one string.
        (drain_label,) = (
            shape
            for shape in layout.top_cell().shapes(layout.layer(67, 5)).each()
            if shape.is_text() and shape.text.string == "Y" and shape.text.y == 2500
        )
        drain_label.text_string = "Y|ybar"
    path = _write_gds(layout, tmp_path / "inv.gds")
    spice = tmp_path / "inv.spice"
    code = main(
        [
            "extract",
            str(path),
            "--deck",
            "sky130",
            "--pins",
            f"VGND,{declared},VPWR",
            "-o",
            str(spice),
            "--format",
            "json",
        ]
    )
    assert code == 0, capsys.readouterr().err
    header = next(
        line
        for line in spice.read_text().splitlines()
        if line.upper().startswith(".SUBCKT TOP ")
    )
    assert header.split()[2:] == expected


def test_cli_rename_declared_pins_keeps_declared_order(tmp_path, capsys):
    """Issue #5534: `--rename-declared-pins` renames a joined-label net to its
    single declared name (a whole-name match), and the header still follows
    `--pins` — the component-label hoist must not regress that path."""
    layout = _make_inverter_layout(extra_y_label="ybar")
    path = str(_write_gds(layout, tmp_path / "inv.gds"))
    out_path = tmp_path / "inv.spice"
    exit_code = main(
        [
            "extract", path, "--deck", "sky130", "-o", str(out_path),
            "--pins", "VPWR,ybar,VGND", "--rename-declared-pins",
            "--format", "json",
        ]
    )  # fmt: skip
    assert exit_code == 0
    capsys.readouterr()
    header = next(
        line for line in out_path.read_text().splitlines()
        if line.lower().startswith(".subckt")
    )  # fmt: skip
    assert header.split()[2:] == ["VPWR", "ybar", "VGND"]
