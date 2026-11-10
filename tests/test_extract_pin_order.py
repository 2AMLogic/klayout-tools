"""``--pins`` order defines the top ``.SUBCKT`` pin order (issue #5255)."""

from __future__ import annotations

from klayout_tools.cli._parsing import parse_declared_pins
from klayout_tools.extract import _apply_pin_order_to_spice


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
