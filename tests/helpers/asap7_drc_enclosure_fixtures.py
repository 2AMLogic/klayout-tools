"""Via-enclosure fixtures for the ASAP7 DRC deck (#3147).

The via-enclosure rules judged on a 26 nm bar around an 18 x 18 nm via live
here: the bar is 4 nm past the via on both sides across it (short of every
threshold) and runs ``e0`` / ``e1`` nm past the via's two ends, so the rule
under test is decided by those two ends alone. The metal on the other side
of the via keeps the long default bar of
:func:`helpers.asap7_drc_fixtures._stack`. They are kept apart from that
module because it is at its file-size ceiling.

- "at least two opposite sides": ``V1.M1.EN.1``, ``V2.M2.EN.1``,
  ``V3.M3.EN.1``;
- "(5 & 5) | (5 & 0)": ``V0.M1.EN.1``, ``V1.M2.EN.2``, ``V2.M3.EN.2``.
"""

from __future__ import annotations

from helpers.asap7_drc_fixtures import (
    _VIA_METALS,
    Case,
    D,
    Shape,
    _fail,
    _flip,
    _pass,
    _stack,
    box,
    poly,
)

#: The "(5 & 5) | (5 & 0)" rules: via -> rule id.
END_CAP_RULES = {"V0": "V0.M1.EN.1", "V1": "V1.M2.EN.2", "V2": "V2.M3.EN.2"}


def bar(e0: float, e1: float, vertical: bool) -> tuple[float, float, float, float]:
    """A 26 nm bar that runs ``e0`` / ``e1`` nm past the via's two ends
    (top / bottom for a vertical bar, left / right for a horizontal one).
    Even at e = 5 nm it stays above the 504 nm^2 M2/M3 area minimum."""
    return (-4, -e0, 22, 18 + e1) if vertical else (-e0, -4, 18 + e1, 22)


def capped(via: str, which: str, e0: float, e1: float, vertical: bool) -> list[Shape]:
    """A via stack whose ``which`` ("lower" or "upper") metal is
    :func:`bar`."""
    return _stack(via, **{which: bar(e0, e1, vertical)})


def two_side_cases() -> list[Case]:
    """Cases for "at least two opposite sides", on the metal below the
    via."""
    c: list[Case] = []
    for via, rule, limits in (
        ("V1", "V1.M1.EN.1", ((5, 2), (2, 5))),
        ("V2", "V2.M2.EN.1", ((5, 5),)),
        ("V3", "V3.M3.EN.1", ((5, 5),)),
    ):
        for vertical in (True, False):
            for e0, e1 in limits:
                c.append(_pass(rule, capped(via, "lower", e0, e1, vertical)))
                for x, y in ((e0 - D, e1), (e0, e1 - D), (e0 - D, e1 - D)):
                    c.append(_fail(rule, capped(via, "lower", x, y, vertical)))
    return c + _v1_notch_cases()


# Each group of abutting M1 boxes forms a single polygon. The full ends
# at y = -8 and 26 enclose V1 by 8 nm. A notch at -3 or 21 leaves 3 nm.
ONE_TOP_NOTCH = (
    (-4, -8, 3, 26),
    (3, -8, 6, 21),
    (6, -8, 22, 26),
)

TWO_TOP_NOTCHES = (
    (-4, -8, 3, 26),
    (3, -8, 6, 21),
    (6, -8, 12, 26),
    (12, -8, 15, 21),
    (15, -8, 22, 26),
)

BOTH_ENDS_NOTCHED = (
    (-4, -8, 3, 26),
    (3, -3, 6, 21),
    (6, -8, 22, 26),
)

# Narrow fingers and opposing notch walls also trip M1 width and spacing.
NOTCH_RULES = ("M1.W.1", "M1.S.4")

V1_PASS = (
    ((bar(8, 8, True),), ()),
    (ONE_TOP_NOTCH, NOTCH_RULES),
    (TWO_TOP_NOTCHES, NOTCH_RULES),
)
V1_FAIL = (
    ((bar(4, 4, True),), ()),
    ((bar(8, 0, True),), ()),
    ((bar(5, 0, True),), ()),
    (BOTH_ENDS_NOTCHED, NOTCH_RULES),
)


def _v1_notch_cases() -> list[Case]:
    """Distinguish two short edges at the same end from one at each end."""
    cases = []
    for make_case, layouts in ((_pass, V1_PASS), (_fail, V1_FAIL)):
        for boxes, also in layouts:
            shapes = _stack("V1", lower=None)
            shapes.extend(box("M1", *bounds) for bounds in boxes)
            case = make_case("V1.M1.EN.1", shapes, also=also)
            cases.extend((case, _flip(case)))
    return cases


def _upper(via: str) -> str:
    """The metal that end-caps the via: M1 for V0, the upper metal of
    ``_VIA_METALS`` otherwise."""
    return "M1" if via == "V0" else _VIA_METALS[via][1]


def _under(via: str, upper: Shape) -> list[Shape]:
    """A via under ``upper`` on the long default lower bar. V0 sits in a
    24 nm LISD bar instead: V0.AUX.1 needs LISD or LIG under it."""
    if via == "V0":
        return [box("V0", 0, 0, 18, 18), box("LISD", -3, -50, 21, 68), upper]
    return [*_stack(via, upper=None), upper]


def _end_capped(via: str, e0: float, e1: float, vertical: bool) -> list[Shape]:
    return _under(via, box(_upper(via), *bar(e0, e1, vertical)))


def _overhang(via: str, vertical: bool) -> list[Shape]:
    """The via sticks one database unit out of its metal's end; the other
    end is long."""
    end = (-4, -50, 22, 18 - D) if vertical else (-50, -4, 18 - D, 22)
    return _under(via, box(_upper(via), *end))


def _notched(via: str) -> list[Shape]:
    """The metal's left end is 5 nm past the via over the lower 14 nm of
    the via and 2 nm past it over the upper 4 nm; the right end is flush.
    The 18 nm step keeps the metal at its minimum width."""
    return _under(
        via,
        poly(
            _upper(via),
            (-50, -4),
            (-5, -4),
            (-5, 14),
            (-2, 14),
            (-2, 22),
            (18, 22),
            (18, -4),
        ),
    )


def end_cap_cases() -> list[Case]:
    """``(5 & 5) | (5 & 0)``: the metal end-caps the via with 5 nm at one
    end and with 5 nm, any length in between, or 0 nm (flush) at the other.
    The DRM states both bounds with ">=", so an opposite end anywhere from
    flush up to (not including) 5 nm meets the "0" bound. Ends are swapped
    and the bar is rotated."""
    c: list[Case] = []
    for via, rule in END_CAP_RULES.items():
        c += [
            _pass(rule, _end_capped(via, 5, 5, False)),
            _pass(rule, _end_capped(via, 5, 5, True)),
            _pass(rule, _end_capped(via, 5, 0, False)),
            _pass(rule, _end_capped(via, 0, 5, False)),
            _pass(rule, _end_capped(via, 5, 0, True)),
            _pass(rule, _end_capped(via, 5, 2.5, False)),
            _pass(rule, _end_capped(via, 5, 5 - D, False)),
            _pass(rule, _end_capped(via, 5 - D, 5, False)),
            _fail(rule, _end_capped(via, 5 - D, 0, False)),
            _fail(rule, _end_capped(via, 0, 5 - D, False)),
            _fail(rule, _end_capped(via, 5 - D, 0, True)),
            _fail(rule, _end_capped(via, 5 - D, 5 - D, False)),
            _fail(rule, _end_capped(via, 5 - D, 5 - D, True)),
            # No side of the via reaches 5 nm.
            _fail(rule, _end_capped(via, 4, 4, False)),
            # A side that is 5 nm over part of its length and 2 nm over the
            # rest is not a 5 nm end.
            _fail(rule, _notched(via)),
        ]
        for vertical in (False, True):
            overhang = _overhang(via, vertical)
            # A V0 that sticks out of M1 still interacts with M1 and LISD
            # (V0.AUX.1); a V1 / V2 is no longer inside the metal above it.
            c.append(
                _fail(rule, overhang)
                if via == "V0"
                else _fail(rule, overhang, also=[f"{via}.AUX.1"])
            )
    return c
