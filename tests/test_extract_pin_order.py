"""``--pins`` order defines the top ``.SUBCKT`` pin order (issue #5255)."""

from __future__ import annotations

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
