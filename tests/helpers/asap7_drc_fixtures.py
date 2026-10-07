"""Generated per-rule fixtures for the ASAP7 DRC deck (issue #2760).

Every rule id in :data:`klayout_tools.decks.asap7_rules.RULES` gets at least
one *fail* case, which must trip that id, and one *pass* case, drawn at the
rule's threshold boundary, which must not. Fixtures are generated here rather
than committed as GDS.

Each :class:`Case` lists the exact set of rule ids it is expected to trip
(``expect``). For almost every case that is ``{rule}`` (fail) or the empty
set (pass). The few exceptions are rules the DRM makes inseparable from
another rule (for example a WELL area below 5832 nm^2 cannot be drawn
without also breaking a WELL width); each such case says why next to it.

Coordinates are nanometres at the DRM's 1x drawing scale; the layout dbu is
0.25 nm (``asap7.lyt``), so ``D`` (one database unit) is the smallest step
past a threshold. :func:`write_cases` places every case in its own 5 um slot
along x in one top cell, so one deck run checks them all and
:func:`slot_of` maps a finding back to its case.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import klayout.db as kdb

#: GDS layer numbers (asap7.lyp "<name> drawing - L/0").
LAYERS = {
    "WELL": 1,
    "FIN": 2,
    "GATE": 7,
    "GCUT": 10,
    "ACTIVE": 11,
    "NSELECT": 12,
    "PSELECT": 13,
    "LIG": 16,
    "LISD": 17,
    "V0": 18,
    "M1": 19,
    "SDT": 88,
    "SLVT": 97,
    "LVT": 98,
    "SRAMDRC": 99,
    "SRAMVT": 110,
}

DBU_UM = 0.00025
#: One database unit, in nm.
D = 0.25
#: Width of each case's slot along x, in nm.
SLOT_NM = 5000.0
#: Offset of a case's origin inside its slot, in nm.
ORIGIN_NM = 1000.0

Point = tuple[float, float]
#: ``(layer, x1, y1, x2, y2)``
Box = tuple[str, float, float, float, float]
#: ``(layer, hull, holes)``
Poly = tuple[str, tuple[Point, ...], tuple[tuple[Point, ...], ...]]
Shape = Box | Poly


@dataclass(frozen=True)
class Case:
    rule: str
    kind: str  # "fail" or "pass"
    shapes: tuple[Shape, ...]
    expect: frozenset[str] = field(default_factory=frozenset)

    @property
    def name(self) -> str:
        return f"{self.rule}-{self.kind}"


def _fail(rule: str, shapes: Iterable[Shape], also: Iterable[str] = ()) -> Case:
    return Case(rule, "fail", tuple(shapes), frozenset({rule, *also}))


def _pass(rule: str, shapes: Iterable[Shape], also: Iterable[str] = ()) -> Case:
    return Case(rule, "pass", tuple(shapes), frozenset(also))


def box(layer: str, x1: float, y1: float, x2: float, y2: float) -> Box:
    return (layer, x1, y1, x2, y2)


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def device(
    *,
    sel: str = "NSELECT",
    ax0: float = 46,
    ax1: float = 116,
    ay0: float = 27,
    ay1: float = 108,
    sen_h: float = 46,
    sen_v: float = 27,
    gate_y0: float = 10,
    gate_y1: float = 125,
    gcut: tuple[float, float, float, float] | None = (0, 112, 162, 135),
    sd: bool = True,
    sdt1: tuple[float, float] = (42, 66),
    lisd1: tuple[float, float] | None = None,
    vt: str | None = None,
    vt_h: float = 46,
    vt_v: float = 27,
) -> list[Shape]:
    """A one-transistor slice modelled on INVx1_ASAP7_75t_R, clean at the
    defaults with most FEOL/MOL rules sitting exactly at their threshold.

    Three 20 nm gates on the 54 nm pitch (x 17, 71, 125); the middle one
    forms the channel. ACTIVE (46..116 x 27..108) extends exactly 25 nm past
    it (GATE.ACTIVE.EX.2) and sits 9 nm from the outer gates
    (GATE.ACTIVE.S.4); the select encloses ACTIVE by exactly 46/27 nm; fins on
    the 27 nm pitch leave exactly 10 nm of ACTIVE past the outer fins; GCUT
    cuts the gates 4 nm above the channel and extends exactly 17 nm past the
    outer gates; SDT/LISD sit exactly 5 nm from the gates and 30 nm apart.
    """
    shapes: list[Shape] = []
    for gx in (17, 71, 125):
        shapes.append(box("GATE", gx, gate_y0, gx + 20, gate_y1))
    shapes.append(box("ACTIVE", ax0, ay0, ax1, ay1))
    shapes.append(box(sel, ax0 - sen_h, ay0 - sen_v, ax1 + sen_h, ay1 + sen_v))
    if vt is not None:
        shapes.append(box(vt, ax0 - vt_h, ay0 - vt_v, ax1 + vt_h, ay1 + vt_v))
    for k in range(5):
        y = 10 + 27 * k
        shapes.append(box("FIN", 0, y, 162, y + 7))
    if gcut is not None:
        shapes.append(box("GCUT", *gcut))
    if sd:
        # LISD keeps the 24 nm INVx1 column (42..66) and always covers SDT.
        l1 = lisd1 if lisd1 is not None else (ay0, ay1)
        shapes.append(box("SDT", sdt1[0], ay0, sdt1[1], ay1))
        shapes.append(box("LISD", min(42, sdt1[0]), l1[0], max(66, sdt1[1]), l1[1]))
        shapes.append(box("SDT", 96, ay0, 120, ay1))
        shapes.append(box("LISD", 96, ay0, 120, ay1))
    return shapes


def active_in(
    sel: str, x1: float, y1: float, x2: float, y2: float, margin_h: float = 46
) -> list[Shape]:
    """An ACTIVE box inside a select drawn with the minimum (46/27 nm)
    enclosure; ``margin_h`` widens the select when the minimum would leave
    it below its own 108 nm width."""
    return [
        box("ACTIVE", x1, y1, x2, y2),
        box(sel, x1 - margin_h, y1 - 27, x2 + margin_h, y2 + 27),
    ]


def gate_pair(y0: float = 0, y1: float = 100) -> list[Shape]:
    """Two 20 nm gates 34 nm apart (each other's GATE.S.3 neighbour)."""
    return [box("GATE", 0, y0, 20, y1), box("GATE", 54, y0, 74, y1)]


def poly(layer: str, *hull: Point) -> Poly:
    return (layer, tuple(hull), ())


def ring(
    layer: str,
    outer: tuple[float, float, float, float],
    hole: tuple[float, float, float, float],
) -> Poly:
    """A box with one rectangular hole."""
    x1, y1, x2, y2 = outer
    hx1, hy1, hx2, hy2 = hole
    return (
        layer,
        ((x1, y1), (x1, y2), (x2, y2), (x2, y1)),
        (((hx1, hy1), (hx2, hy1), (hx2, hy2), (hx1, hy2)),),
    )


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


def _well_cases() -> list[Case]:
    c: list[Case] = []
    c += [
        _fail("WELL.W.1", [box("WELL", 0, 0, 108 - D, 60)]),
        _pass("WELL.W.1", [box("WELL", 0, 0, 108, 60)]),
    ]
    c += [
        _fail("WELL.W.2", [box("WELL", 0, 0, 120, 54 - D)]),
        _pass("WELL.W.2", [box("WELL", 0, 0, 120, 54)]),
    ]
    for gap, mk in ((108 - D, _fail), (108, _pass)):
        c.append(
            mk(
                "WELL.S.1",
                [box("WELL", 0, 0, 120, 60), box("WELL", 0, 60 + gap, 120, 120 + gap)],
            )
        )
    for gap, mk in ((54 - D, _fail), (54, _pass)):
        c.append(
            mk(
                "WELL.S.2",
                [box("WELL", 0, 0, 120, 60), box("WELL", 120 + gap, 0, 240 + gap, 60)],
            )
        )
    # The minimum widths (108 x 54) already give the minimum area (DRM 3.2,
    # note 1), so a too-small WELL also breaks WELL.W.1.
    c.append(_fail("WELL.A.1A", [box("WELL", 0, 0, 108 - D, 54)], also=["WELL.W.1"]))
    c.append(_pass("WELL.A.1A", [box("WELL", 0, 0, 108, 54)]))
    # Likewise a hole below 5832 nm^2 is narrower than the 108 nm vertical
    # WELL spacing.
    c.append(
        _fail(
            "WELL.A.1B",
            [ring("WELL", (0, 0, 300, 300), (123, 96, 177, 204 - D))],
            also=["WELL.S.1"],
        )
    )
    c.append(_pass("WELL.A.1B", [ring("WELL", (0, 0, 300, 300), (123, 96, 177, 204))]))
    for m, mk in ((7 - D, _fail), (7, _pass)):
        c.append(mk("WELL.GATE.EX.1", [*gate_pair(), box("WELL", -m, -20, 150, 120)]))
        c.append(mk("WELL.GATE.EX.2", [*gate_pair(), box("WELL", -20, -m, 150, 120)]))
    return c


def _fin_cases() -> list[Case]:
    c: list[Case] = []
    c.append(_fail("FIN.W.1", [box("FIN", 0, 0, 120, 7 + D)]))
    c.append(_fail("FIN.W.1", [box("FIN", 0, 0, 120, 7 - D)]))
    c.append(_pass("FIN.W.1", [box("FIN", 0, 0, 120, 7)]))
    c += [
        _fail("FIN.W.2", [box("FIN", 0, 0, 108 - D, 7)]),
        _pass("FIN.W.2", [box("FIN", 0, 0, 108, 7)]),
    ]
    for gap, mk in ((20 + D, _fail), (20 - D, _fail), (20, _pass), (47, _pass)):
        c.append(
            mk(
                "FIN.S.1",
                [box("FIN", 0, 0, 120, 7), box("FIN", 0, 7 + gap, 120, 14 + gap)],
            )
        )
    notched = poly(
        "FIN",
        (0, 0),
        (0, 7),
        (110, 7),
        (110, 3.5),
        (130, 3.5),
        (130, 7),
        (240, 7),
        (240, 0),
    )
    c += [_fail("FIN.AUX.1", [notched]), _pass("FIN.AUX.1", [box("FIN", 0, 0, 240, 7)])]
    return c


def _gate_cases() -> list[Case]:
    c: list[Case] = []
    c.append(
        _fail(
            "GATE.W.1",
            [box("GATE", 0, 0, 20 + D, 100), box("GATE", 54 + D, 0, 74 + D, 100)],
        )
    )
    c.append(
        _fail(
            "GATE.W.1",
            [box("GATE", 0, 0, 20 - D, 100), box("GATE", 54 - D, 0, 74 - D, 100)],
        )
    )
    c.append(_pass("GATE.W.1", gate_pair()))
    c += [_fail("GATE.W.2", gate_pair(0, 40 - D)), _pass("GATE.W.2", gate_pair(0, 40))]

    def four(gap: float) -> list[Shape]:
        # Pairs 34 nm apart (GATE.S.3 neighbours), the pairs `gap` apart.
        x2 = 74 + gap
        return [
            *gate_pair(),
            box("GATE", x2, 0, x2 + 20, 100),
            box("GATE", x2 + 54, 0, x2 + 74, 100),
        ]

    c += [_fail("GATE.S.1", four(88 - D)), _pass("GATE.S.1", four(88))]
    c.append(
        _fail(
            "GATE.S.2",
            [box("GATE", 0, 0, 20, 100), box("GATE", 54 - D, 0, 74 - D, 100)],
        )
    )
    c.append(_pass("GATE.S.2", gate_pair()))
    c += [
        _fail("GATE.S.3", [box("GATE", 0, 0, 20, 100)]),
        _pass("GATE.S.3", gate_pair()),
    ]
    notched = poly(
        "GATE",
        (0, 0),
        (0, 90),
        (10, 90),
        (10, 110),
        (0, 110),
        (0, 200),
        (20, 200),
        (20, 0),
    )
    c.append(_fail("GATE.AUX.1", [notched, box("GATE", 54, 0, 74, 200)]))
    c.append(_pass("GATE.AUX.1", gate_pair(0, 200)))
    # ACTIVE's left edge inside the first gate of a standalone channel.
    aux3 = [
        *gate_pair(0, 150),
        box("ACTIVE", 10, 30, 99, 120),
        box("NSELECT", -36, 3, 145, 147),
    ]
    c.append(_fail("GATE.ACTIVE.AUX.3", aux3))
    c.append(_pass("GATE.ACTIVE.AUX.3", device()))
    c += [
        _fail("GATE.ACTIVE.EX.1", device(gate_y0=27 - 4 + D)),
        _pass("GATE.ACTIVE.EX.1", device(gate_y0=27 - 4)),
        _fail("GATE.ACTIVE.EX.2", device(ax0=46 + D)),
        _pass("GATE.ACTIVE.EX.2", device()),
        _fail("GATE.ACTIVE.S.4", device(ax0=46 - D)),
        _pass("GATE.ACTIVE.S.4", device()),
    ]
    return c


def _active_cases() -> list[Case]:
    c: list[Case] = []
    c += [
        _fail("ACTIVE.FIN.EX.1", device(ay1=108 - D)),
        _pass("ACTIVE.FIN.EX.1", device()),
    ]
    c.append(_fail("ACTIVE.W.1", active_in("NSELECT", 0, 0, 70, 27 - D)))
    c.append(_pass("ACTIVE.W.1", active_in("NSELECT", 0, 0, 70, 27)))
    c.append(_fail("ACTIVE.W.3", active_in("NSELECT", 0, 0, 16 - D, 81, margin_h=50)))
    c.append(_pass("ACTIVE.W.3", active_in("NSELECT", 0, 0, 16, 81, margin_h=50)))
    for gap, mk in ((27 - D, _fail), (27, _pass)):
        sel = box("NSELECT", -46, -27, 116, 81 + gap + 27)
        c.append(
            mk(
                "ACTIVE.S.1",
                [
                    box("ACTIVE", 0, 0, 70, 40),
                    box("ACTIVE", 0, 40 + gap, 70, 81 + gap),
                    sel,
                ],
            )
        )
    for gap, mk in ((38 - D, _fail), (38, _pass)):
        sel = box("NSELECT", -46, -27, 140 + gap + 46, 108)
        c.append(
            mk(
                "ACTIVE.S.2B",
                [
                    box("ACTIVE", 0, 0, 70, 81),
                    box("ACTIVE", 70 + gap, 0, 140 + gap, 81),
                    sel,
                ],
            )
        )
    for gap, mk in ((27 - D, _fail), (27, _pass)):
        c.append(
            mk(
                "ACTIVE.WELL.S.4",
                [
                    *active_in("NSELECT", 0, 0, 70, 81),
                    box("WELL", 70 + gap, 0, 300, 81),
                ],
            )
        )
    for m, mk in ((27 - D, _fail), (27, _pass)):
        c.append(
            mk(
                "ACTIVE.WELL.EN.1",
                [
                    *active_in("PSELECT", 0, 0, 70, 81),
                    box("WELL", -m, -m, 70 + 60, 81 + 60),
                ],
            )
        )
    c.append(_fail("ACTIVE.A.1A", active_in("NSELECT", 0, 0, 32 - D, 27)))
    c.append(_pass("ACTIVE.A.1A", active_in("NSELECT", 0, 0, 32, 27)))
    # Any ACTIVE hole is at least 38 x 27 nm (ACTIVE.S.2B / ACTIVE.S.1),
    # i.e. 1026 nm^2 > 864 nm^2, so a hole at or just under the area
    # threshold also breaks the horizontal spacing; the pass case shows the
    # 864 nm^2 boundary itself does not trip ACTIVE.A.1B.
    ring_sel = box("NSELECT", -46, -27, 246, 147)
    c.append(
        _fail(
            "ACTIVE.A.1B",
            [ring("ACTIVE", (0, 0, 200, 120), (84, 46, 116 - D, 73)), ring_sel],
            also=["ACTIVE.S.2B"],
        )
    )
    c.append(
        _pass(
            "ACTIVE.A.1B",
            [ring("ACTIVE", (0, 0, 200, 120), (84, 46, 116, 73)), ring_sel],
            also=["ACTIVE.S.2B"],
        )
    )
    c.append(_fail("ACTIVE.AUX.1", [box("ACTIVE", 0, 0, 70, 81)]))
    straddle = [
        box("ACTIVE", 0, 0, 70, 81),
        box("NSELECT", -46, -27, 116, 40),
        box("PSELECT", -46, 40, 116, 108),
    ]
    c.append(_fail("ACTIVE.AUX.1", straddle))
    c.append(_pass("ACTIVE.AUX.1", device()))
    return c


def _gcut_cases() -> list[Case]:
    c: list[Case] = []
    c += [
        _fail("GCUT.W.1", device(gcut=(0, 112, 162, 129 - D))),
        _pass("GCUT.W.1", device(gcut=(0, 112, 162, 129))),
    ]
    # Over the channel gate, GCUT-to-channel spacing *is* that gate's
    # extension past ACTIVE, so both rules trip together.
    c.append(
        _fail(
            "GCUT.ACTIVE.S.1",
            device(gcut=(0, 112 - D, 162, 135)),
            also=["GATE.ACTIVE.EX.1"],
        )
    )
    c.append(_pass("GCUT.ACTIVE.S.1", device()))
    c += [
        _fail("GCUT.GATE.EX.1", device(gcut=(D, 112, 162, 135))),
        _pass("GCUT.GATE.EX.1", device()),
    ]
    for x1, mk in ((162 + D, _fail), (162, _pass)):
        c.append(
            mk(
                "GCUT.GATE.S.2",
                [*device(gcut=(0, 112, x1, 135)), box("GATE", 179, 10, 199, 125)],
            )
        )
    for gap, mk in ((35 - D, _fail), (35, _pass)):
        cuts = [box("GCUT", -17, 50, 91, 67), box("GCUT", -17, 67 + gap, 91, 84 + gap)]
        c.append(mk("GCUT.S.3", [*gate_pair(0, 200), *cuts]))
    c += [
        _fail("GCUT.AUX.1", [box("GCUT", 0, 0, 100, 20)]),
        _pass("GCUT.AUX.1", device()),
    ]
    c.append(_fail("GCUT.AUX.2", [*gate_pair(0, 200), box("GCUT", 10, 50, 91, 67)]))
    c.append(_pass("GCUT.AUX.2", [*gate_pair(0, 200), box("GCUT", -17, 50, 91, 67)]))
    c += [
        _fail("GCUT.AUX.3", device(gcut=(0, 100, 162, 135))),
        _pass("GCUT.AUX.3", device()),
    ]
    return c


def _select_cases() -> list[Case]:
    c: list[Case] = []
    for name in ("NSELECT", "PSELECT", "SLVT", "LVT", "SRAMVT"):
        c += [
            _fail(f"{name}.W.1", [box(name, 0, 0, 108 - D, 60)]),
            _pass(f"{name}.W.1", [box(name, 0, 0, 108, 60)]),
        ]
        c += [
            _fail(f"{name}.W.2", [box(name, 0, 0, 120, 54 - D)]),
            _pass(f"{name}.W.2", [box(name, 0, 0, 120, 54)]),
        ]
        if name in ("NSELECT", "PSELECT"):
            c.append(_fail(f"{name}.ACTIVE.EN.1", device(sel=name, sen_h=46 - D)))
            c.append(_pass(f"{name}.ACTIVE.EN.1", device(sel=name)))
            c.append(_fail(f"{name}.ACTIVE.EN.2", device(sel=name, sen_v=27 - D)))
            c.append(_pass(f"{name}.ACTIVE.EN.2", device(sel=name)))
        else:
            c.append(_fail(f"{name}.ACTIVE.EN.1", device(vt=name, vt_h=46 - D)))
            c.append(_pass(f"{name}.ACTIVE.EN.1", device(vt=name)))
            c.append(_fail(f"{name}.ACTIVE.EN.2", device(vt=name, vt_v=27 - D)))
            c.append(_pass(f"{name}.ACTIVE.EN.2", device(vt=name)))
        for m, mk in ((7 - D, _fail), (7, _pass)):
            c.append(
                mk(
                    f"{name}.GATE.EX.1",
                    [*gate_pair(0, 100), box(name, -m, -20, 120, 120)],
                )
            )
            c.append(
                mk(
                    f"{name}.GATE.EX.2",
                    [*gate_pair(0, 100), box(name, -20, -m, 120, 120)],
                )
            )
    c.append(
        _fail(
            "NSELECT.PSELECT.AUX.1",
            [box("NSELECT", 0, 0, 120, 60), box("PSELECT", 0, 50, 120, 110)],
        )
    )
    c.append(
        _pass(
            "NSELECT.PSELECT.AUX.1",
            [box("NSELECT", 0, 0, 120, 60), box("PSELECT", 0, 60, 120, 120)],
        )
    )
    c.append(
        _fail("VT.AUX.2", [box("SLVT", 0, 0, 120, 60), box("LVT", 0, 50, 120, 110)])
    )
    c.append(
        _pass("VT.AUX.2", [box("SLVT", 0, 0, 120, 60), box("LVT", 0, 60, 120, 120)])
    )
    return c


def _sdt_cases() -> list[Case]:
    c: list[Case] = []
    c += [_fail("SDT.W.1", device(sdt1=(42, 66 - D))), _pass("SDT.W.1", device())]
    # SDT edges sit on ACTIVE edges (SDT.ACTIVE.AUX.2) and SDT sits inside
    # LISD (SDT.LISD.AUX.4), so a short SDT is also a short ACTIVE, a short
    # SDT/ACTIVE overlap and a short SDT/LISD overlap.
    short = ["SDT.W.2", "SDT.ACTIVE.OV.1", "SDT.LISD.OV.2", "ACTIVE.W.1"]

    def flat_sd(h: float) -> list[Shape]:
        return [
            *active_in("NSELECT", 0, 0, 70, h),
            box("SDT", 10, 0, 34, h),
            box("LISD", 10, 0, 34, 60),
        ]

    for rule in short[:3]:
        c.append(_fail(rule, flat_sd(27 - D), also=[r for r in short if r != rule]))
        c.append(_pass(rule, flat_sd(27)))
    for gap, mk in ((30 - D, _fail), (30, _pass)):
        sds = [box("SDT", 10, 0, 34, 81), box("LISD", 10, 0, 34, 81)]
        sds += [
            box("SDT", 34 + gap, 0, 58 + gap, 81),
            box("LISD", 34 + gap, 0, 58 + gap, 81),
        ]
        c.append(mk("SDT.S.1", [*active_in("NSELECT", 0, 0, 120, 81), *sds]))
    c += [
        _fail("SDT.GATE.S.2", device(sdt1=(42 - D, 66 - D))),
        _pass("SDT.GATE.S.2", device()),
    ]
    # Overlap trips SDT.GATE.AUX.1 alone; touching is also a 0 nm spacing.
    c.append(_fail("SDT.GATE.AUX.1", device(sdt1=(36, 60))))
    c.append(_fail("SDT.GATE.AUX.1", device(sdt1=(37, 61)), also=["SDT.GATE.S.2"]))
    c.append(_pass("SDT.GATE.AUX.1", device()))
    aux2 = [s for s in device() if not (s[0] in ("SDT", "LISD") and s[1] == 42)]
    aux2 += [box("SDT", 42, 27, 66, 108 + D), box("LISD", 42, 27, 66, 108 + D)]
    c += [_fail("SDT.ACTIVE.AUX.2", aux2), _pass("SDT.ACTIVE.AUX.2", device())]
    # An SDT entirely outside ACTIVE cannot have its horizontal edges on
    # ACTIVE's, so SDT.ACTIVE.AUX.2 trips with it; abutting is still outside.
    outside = [
        *active_in("NSELECT", 0, 0, 70, 81),
        box("SDT", 10, 81, 34, 162),
        box("LISD", 10, 81, 34, 162),
    ]
    c.append(_fail("SDT.ACTIVE.AUX.3", outside, also=["SDT.ACTIVE.AUX.2"]))
    c.append(_pass("SDT.ACTIVE.AUX.3", device()))
    c += [
        _fail("SDT.LISD.AUX.4", device(lisd1=(27, 108 - D))),
        _pass("SDT.LISD.AUX.4", device()),
    ]
    return c


def _lisd_lig_cases(layer: str, width: float) -> list[Case]:
    """Width and length-conditioned spacing for LISD (24 nm) and LIG (16 nm)."""
    c: list[Case] = []
    w = width
    c += [
        _fail(f"{layer}.W.1", [box(layer, 0, 0, w - D, 100)]),
        _pass(f"{layer}.W.1", [box(layer, 0, 0, w, 100)]),
    ]
    for gap, mk in ((18 - D, _fail), (18, _pass)):
        c.append(
            mk(
                f"{layer}.S.1",
                [box(layer, 0, 0, w, 100), box(layer, w + gap, 0, 2 * w + gap, 100)],
            )
        )
    for gap, mk in ((25 - D, _fail), (25, _pass)):
        c.append(
            mk(
                f"{layer}.S.2",
                [
                    box(layer, 0, 0, 24, 100),
                    box(layer, -100, 100 + gap, 124, 100 + gap + w),
                ],
            )
        )
    for gap, mk in ((27 - D, _fail), (27, _pass)):
        c.append(
            mk(
                f"{layer}.S.3",
                [box(layer, 0, 0, 24, 100), box(layer, 0, 100 + gap, 24, 200 + gap)],
            )
        )
    return c


def _lisd_cases() -> list[Case]:
    c = _lisd_lig_cases("LISD", 24)
    c += [
        _fail("LISD.A.1", [box("LISD", 0, 0, 24, 27 - D)]),
        _pass("LISD.A.1", [box("LISD", 0, 0, 24, 27)]),
    ]
    return c


def _lig_cases() -> list[Case]:
    c = _lisd_lig_cases("LIG", 16)
    for gap, mk in ((31 - D, _fail), (31, _pass)):
        c.append(
            mk(
                "LIG.S.4",
                [box("LIG", 0, 0, 16, 100), box("LIG", 0, 100 + gap, 16, 200 + gap)],
            )
        )
        c.append(
            mk(
                "LIG.S.5",
                [box("LIG", 0, 0, 24, 100), box("LIG", 0, 100 + gap, 16, 200 + gap)],
            )
        )
    for gap, mk in ((14 - D, _fail), (14, _pass)):
        c.append(
            mk(
                "LIG.GATE.S.9A",
                [*gate_pair(), box("LIG", -17, 100 + gap, 91, 116 + gap)],
            )
        )
    for gap, mk in ((17 - D, _fail), (17, _pass)):
        c.append(
            mk("LIG.GATE.S.9B", [*gate_pair(), box("LIG", 74 + gap, 0, 90 + gap, 100)])
        )
    # One channel gate (x 0..20) with its neighbour; LIG diagonally off the
    # channel gate's top-left corner, 3 nm left and `dy` above it, so only
    # the all-direction (Euclidean) LIG.GATE.S.10 can see it.
    channel = [
        *gate_pair(0, 121),
        box("ACTIVE", -25, 20, 45, 101),
        box("NSELECT", -71, -7, 91, 128),
    ]
    c.append(_fail("LIG.GATE.S.10", [*channel, box("LIG", -19, 124.75, -3, 160)]))
    c.append(_pass("LIG.GATE.S.10", [*channel, box("LIG", -19, 125, -3, 160)]))
    cut = [*gate_pair(0, 200), box("GCUT", -17, 100, 91, 117)]
    for gap, mk in ((5 - D, _fail), (5, _pass)):
        c.append(mk("LIG.GCUT.S.11", [*cut, box("LIG", -17, 117 + gap, 91, 133 + gap)]))
    c += [
        _fail("LIG.A.1", [box("LIG", 0, 0, 18, 18 - D)]),
        _pass("LIG.A.1", [box("LIG", 0, 0, 18, 18)]),
    ]
    # LIG overlapping the top-right corner of a LISD bar by 8 nm x `h`.
    for h, mk in ((16 - D, _fail), (16, _pass)):
        c.append(
            mk(
                "LIG.LISD.A.2",
                [box("LISD", 0, 0, 24, 100), box("LIG", 16, 100 - h, 116, 116 - h)],
            )
        )
    for ov, mk in ((8 - D, _fail), (8, _pass)):
        c.append(
            mk(
                "LIG.LISD.OV.1",
                [box("LISD", 0, 0, 24, 100), box("LIG", 24 - ov, 40, 124 - ov, 60)],
            )
        )
    for h, mk in ((16 - D, _fail), (16, _pass)):
        c.append(
            mk("LIG.GATE.A.3", [*gate_pair(), box("LIG", -17, 100 - h, 91, 116 - h)])
        )
    c.append(_fail("LIG.GATE.AUX.1", [*gate_pair(), box("LIG", 10, 40, 91, 72)]))
    c.append(_pass("LIG.GATE.AUX.1", [*gate_pair(), box("LIG", -17, 40, 91, 72)]))
    for ext, mk in ((1 - D, _fail), (1, _pass)):
        c.append(mk("LIG.GATE.EX.1", [*gate_pair(), box("LIG", -ext, 40, 91, 56)]))
    return c


def _v0_cases() -> list[Case]:
    def via(w: float, under: str | None = "LISD") -> list[Shape]:
        shapes = [box("V0", 3, 40, 3 + w, 58), box("M1", -50, 40, 100, 58)]
        if under is not None:
            shapes.append(box(under, 0, 0, 24, 100))
        return shapes

    return [
        _fail("V0.W.1", via(18 - D)),
        _pass("V0.W.1", via(18)),
        _fail("V0.AUX.1", via(18, under=None)),
        _pass("V0.AUX.1", via(18)),
    ]


def _geometry_cases() -> list[Case]:
    chamfered = poly("WELL", (0, 0), (0, 300), (240, 300), (300, 240), (300, 0))
    return [
        _fail("GEOMETRY.NONORTHOGONAL", [chamfered]),
        _pass("GEOMETRY.NONORTHOGONAL", [box("WELL", 0, 0, 300, 300)]),
    ]


def all_cases() -> list[Case]:
    """Every fixture case, fail and pass, for every rule."""
    return [
        *_geometry_cases(),
        *_well_cases(),
        *_fin_cases(),
        *_gate_cases(),
        *_active_cases(),
        *_gcut_cases(),
        *_select_cases(),
        *_sdt_cases(),
        *_lisd_cases(),
        *_lig_cases(),
        *_v0_cases(),
    ]


# --------------------------------------------------------------------------
# Layout writing
# --------------------------------------------------------------------------


def _to_dbu(v: float) -> int:
    q = v / D
    assert abs(q - round(q)) < 1e-9, f"{v} nm is off the 0.25 nm grid"
    return int(round(q))


def _insert(
    layout: kdb.Layout, cell: kdb.Cell, shape: Shape, dx: float, scale: float
) -> None:
    layer = layout.layer(LAYERS[shape[0]], 0)
    if len(shape) == 5:
        _, x1, y1, x2, y2 = shape  # type: ignore[misc]
        cell.shapes(layer).insert(
            kdb.Box(
                _to_dbu((x1 + dx) * scale),
                _to_dbu(y1 * scale),
                _to_dbu((x2 + dx) * scale),
                _to_dbu(y2 * scale),
            )
        )
        return
    _, hull, holes = shape  # type: ignore[misc]

    def pts(points: Sequence[Point]) -> list[kdb.Point]:
        return [
            kdb.Point(_to_dbu((x + dx) * scale), _to_dbu(y * scale)) for x, y in points
        ]

    polygon = kdb.Polygon(pts(hull))
    for hole in holes:
        polygon.insert_hole(pts(hole))
    cell.shapes(layer).insert(polygon)


def write_shapes(path: str, shapes: Sequence[Shape], scale: float = 1.0) -> None:
    """Write one set of shapes as a single-top-cell GDS (``scale`` multiplies
    every coordinate; 4.0 draws the same geometry at 4x)."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("TOP")
    for s in shapes:
        _insert(layout, top, s, 0.0, scale)
    layout.write(path)


def write_cases(path: str, cases: Sequence[Case]) -> None:
    """Write every case into its own slot of one GDS."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("TOP")
    for i, case in enumerate(cases):
        for s in case.shapes:
            _insert(layout, top, s, i * SLOT_NM + ORIGIN_NM, 1.0)
    layout.write(path)


def slot_of(bbox: dict[str, int]) -> int:
    """Index of the case whose slot holds a finding's bbox (dbu)."""
    return int((bbox["left"] * D) // SLOT_NM)
