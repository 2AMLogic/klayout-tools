"""Generated per-rule fixtures for the ASAP7 DRC deck (#2760, #2810).

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
past a threshold. :func:`write_cases` places every case in its own 5.376 um slot
along x in one top cell, so one deck run checks them all and
:func:`slot_of` maps a finding back to its case.
"""

from __future__ import annotations

import math
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
    "M2": 20,
    "V1": 21,
    "V2": 25,
    "M3": 30,
    "V3": 35,
    "M4": 40,
    "V4": 45,
    "M5": 50,
    "V5": 55,
    "M6": 60,
    "V6": 65,
    "M7": 70,
    "V7": 75,
    "M8": 80,
    "V8": 85,
    "M9": 90,
    "V9": 95,
    "SDT": 88,
    "SLVT": 97,
    "LVT": 98,
    "SRAMDRC": 99,
    "SRAMVT": 110,
}

DBU_UM = 0.00025
#: One database unit, in nm.
D = 0.25
#: Width of each case's slot along x, in nm. Slot and origin are multiples
#: of 192 nm (the least common multiple of the M4-M7 grids and track
#: pitches, 24 / 32 / 48 / 64 nm), so the absolute-grid rules (M5.AUX.1,
#: M7.AUX.2, ...) see the same x coordinates in every slot.
SLOT_NM = 5376.0
#: Offset of a case's origin inside its slot, in nm.
ORIGIN_NM = 1152.0

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

    c = [
        _fail("V0.W.1", via(18 - D)),
        _pass("V0.W.1", via(18)),
        _fail("V0.AUX.1", via(18, under=None)),
        _pass("V0.AUX.1", via(18)),
    ]

    # V0 landing on the end of an 18 nm LIG bar (x 0..100) under an M1 bar:
    # the V0/LIG overlap is `ov` x 18 nm, 288 nm^2 at ov = 16 nm.
    def on_lig(ov: float) -> list[Shape]:
        x0 = 100 - ov
        return [
            box("LIG", 0, 0, 100, 18),
            box("V0", x0, 0, x0 + 18, 18),
            box("M1", -50, 0, 150, 18),
        ]

    c += [_fail("V0.LIG.A.1", on_lig(16 - D)), _pass("V0.LIG.A.1", on_lig(16))]
    # A V0 inside LISD is exempt even when its LIG overlap is small (here
    # 5 x 18 = 90 nm^2): the rule is scoped to V0 not inside LISD. The LIG
    # overlaps LISD by exactly the LIG.LISD.OV.1 / LIG.LISD.A.2 minimums.
    c.append(
        _pass(
            "V0.LIG.A.1",
            [
                box("LISD", 0, 0, 24, 100),
                box("V0", 3, 40, 21, 58),
                box("LIG", 16, 40, 116, 58),
                box("M1", -50, 40, 100, 58),
            ],
        )
    )
    return c


def _metal_cases(layer: str) -> list[Case]:
    """Table 3.12.1, identical for M1, M2 and M3."""
    c: list[Case] = []
    c += [
        _fail(f"{layer}.W.1", [box(layer, 0, 0, 18 - D, 100)]),
        _pass(f"{layer}.W.1", [box(layer, 0, 0, 18, 100)]),
    ]
    for gap, mk in ((18 - D, _fail), (18, _pass)):
        c.append(
            mk(
                f"{layer}.S.1",
                [box(layer, 0, 0, 18, 100), box(layer, 18 + gap, 0, 36 + gap, 100)],
            )
        )
    # The side-to-side rule is not axis-bound: the same pair rotated by 90
    # degrees (horizontal bars) trips it too.
    c.append(
        _fail(
            f"{layer}.S.1",
            [box(layer, 0, 0, 100, 18), box(layer, 0, 36 - D, 100, 54 - D)],
        )
    )
    for gap, mk in ((25 - D, _fail), (25, _pass)):
        c.append(
            mk(
                f"{layer}.S.2",
                [
                    box(layer, 0, 0, 24, 100),
                    box(layer, -100, 100 + gap, 124, 118 + gap),
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
    for gap, mk in ((31 - D, _fail), (31, _pass)):
        c.append(
            mk(
                f"{layer}.S.4",
                [box(layer, 0, 0, 18, 100), box(layer, 0, 100 + gap, 18, 200 + gap)],
            )
        )
        c.append(
            mk(
                f"{layer}.S.5",
                [box(layer, 0, 0, 24, 100), box(layer, 0, 100 + gap, 18, 200 + gap)],
            )
        )
    # Corner-to-corner: 12 nm across, `dy` up, no projected overlap; 20 nm
    # Euclidean at dy = 16 nm.
    for dy, mk in ((16 - D, _fail), (16, _pass)):
        c.append(
            mk(
                f"{layer}.S.6",
                [box(layer, 0, 0, 100, 40), box(layer, 112, 40 + dy, 212, 80 + dy)],
            )
        )
    # Side-to-side bars at exactly 18 nm whose facing edges overlap by one
    # database unit are a side-to-side pair, not a corner pair: no S.6.
    c.append(
        _pass(
            f"{layer}.S.6",
            [box(layer, 0, 0, 100, 40), box(layer, 100 - D, 58, 200 - D, 98)],
        )
    )
    c += [
        _fail(f"{layer}.A.1", [box(layer, 0, 0, 18, 28 - D)]),
        _pass(f"{layer}.A.1", [box(layer, 0, 0, 18, 28)]),
    ]
    return c


#: Via -> (metal below, metal above), DRM section 3.13.
_VIA_METALS = {"V1": ("M1", "M2"), "V2": ("M2", "M3"), "V3": ("M3", "M4")}


def _stack(
    via: str,
    w: float = 18,
    lower: tuple[float, float, float, float] | None = (0, -50, 18, 68),
    upper: tuple[float, float, float, float] | str | None = "track",
    via_y0: float = 0,
) -> list[Shape]:
    """A ``w`` x 18 nm via at ``(0, via_y0)`` on a vertical lower-metal bar
    and a horizontal upper-metal bar (``None`` omits that metal). Both bars
    are 18 nm wide and flush with the via across their track, except M4
    above V3, drawn as a legal minimum-width M4 track: 24 nm tall on track
    0 (M4.W.1, M4.AUX.1, M4.AUX.2)."""
    lo, up = _VIA_METALS[via]
    if upper == "track":
        upper = (-50, 0, 68, 24 if up == "M4" else 18)
    shapes: list[Shape] = [box(via, 0, via_y0, w, via_y0 + 18)]
    if lower is not None:
        shapes.append(box(lo, *lower))
    if upper is not None:
        assert not isinstance(upper, str)
        shapes.append(box(up, *upper))
    return shapes


def _via_cases() -> list[Case]:
    c: list[Case] = []
    for via in _VIA_METALS:
        c += [
            _fail(f"{via}.W.1", _stack(via, 18 - D)),
            _pass(f"{via}.W.1", _stack(via)),
        ]
        # Partly off the upper metal: the via sticks out 1 nm across it. An
        # M4 track cannot move 1 nm off its grid, so under M4 the via moves.
        off = (
            _stack(via, via_y0=-1)
            if via == "V3"
            else _stack(via, upper=(-50, 1, 68, 19))
        )
        c += [
            _fail(f"{via}.AUX.1", _stack(via, upper=None)),
            _fail(f"{via}.AUX.1", _stack(via, lower=None)),
            _fail(f"{via}.AUX.1", off),
            _pass(f"{via}.AUX.1", _stack(via)),
        ]

    # "At least two opposite sides": the enclosing metal is a 26 nm bar
    # (4 nm past the via on both sides across it, short of every threshold)
    # that extends `e0` / `e1` past the via at its two ends -- top/bottom
    # for a vertical bar, left/right for a horizontal one. The 26 nm width
    # keeps the bar above the 504 nm^2 M2/M3 area minimum even at e = 5 nm.
    # The other metal keeps its long default bar.
    def capped(
        via: str, which: str, e0: float, e1: float, vertical: bool
    ) -> list[Shape]:
        bar = (-4, -e0, 22, 18 + e1) if vertical else (-e0, -4, 18 + e1, 22)
        return _stack(via, **{which: bar})

    for rule, via, which, e in (
        ("V2.M2.EN.1", "V2", "lower", 5),
        ("V3.M3.EN.1", "V3", "lower", 5),
    ):
        for vertical in (True, False):
            c.append(_fail(rule, capped(via, which, e - D, e - D, vertical)))
            c.append(_pass(rule, capped(via, which, e, e, vertical)))
        # Short at one end only is still short on that pair.
        c.append(_fail(rule, capped(via, which, e, e - D, True)))

    # V3.M4.EN.2 (11 nm) on a legal M4 track (24 nm tall on track 0: 0 / 6
    # nm past the via vertically). M4.W.5 makes M4 at least 44 nm long, so
    # the two ends share 26 nm: one end exactly 11 nm passes, one dbu less
    # at either end fails. (A vertically enclosing M4 would be 72 nm tall
    # and at least 44 nm wide, i.e. >= 13 nm past the via on both pairs, so
    # no M4-legal shape fails in that orientation.)
    def m4_capped(e0: float, e1: float) -> list[Shape]:
        return _stack("V3", upper=(-e0, 0, 18 + e1, 24))

    c += [
        _fail("V3.M4.EN.2", m4_capped(11 - D, 15 + D)),
        _fail("V3.M4.EN.2", m4_capped(15 + D, 11 - D)),
        _pass("V3.M4.EN.2", m4_capped(11, 15)),
        _pass("V3.M4.EN.2", m4_capped(15, 11)),
    ]
    return c


def _transpose(shapes: Iterable[Shape]) -> list[Shape]:
    """Mirror shapes about the x = y diagonal (a horizontal bar becomes a
    vertical one), to draw a vertically routed layer's cases from the
    horizontally routed layer's."""
    out: list[Shape] = []
    for s in shapes:
        if len(s) == 5:
            layer, x1, y1, x2, y2 = s  # type: ignore[misc]
            out.append(box(layer, y1, x1, y2, x2))
        else:
            layer, hull, holes = s  # type: ignore[misc]
            out.append(
                (
                    layer,
                    tuple((y, x) for x, y in hull),
                    tuple(tuple((y, x) for x, y in h) for h in holes),
                )
            )
    return out


def _flip(case: Case) -> Case:
    return Case(case.rule, case.kind, tuple(_transpose(case.shapes)), case.expect)


#: M4-M7 -> (routing direction, minimum width = grid g, W.2 maximum), DRM
#: Tables 3.14.1-3.14.2 / 3.16.1-3.16.2.
_SADP = {
    "M4": ("h", 24, 480),
    "M5": ("v", 24, 480),
    "M6": ("h", 32, 640),
    "M7": ("v", 32, 640),
}


def _sadp_metal_cases(layer: str) -> list[Case]:
    """Drawn for a horizontally routed layer (bars along x, on track 0 at
    y = 0) and transposed for M5 / M7."""
    direction, g, wmax = _SADP[layer]

    def bar(y0: float, h: float, x0: float = 0, x1: float = 200) -> Box:
        return box(layer, x0, y0, x1, y0 + h)

    def ids(*suffixes: str) -> list[str]:
        return [f"{layer}.{x}" for x in suffixes]

    r = f"{layer}."
    c: list[Case] = [
        # With both horizontal edges on the g grid (AUX.1) every width is a
        # multiple of g: a width one dbu off any multiple is inseparable
        # from an off-grid edge.
        _fail(r + "W.1", [bar(0, g - D)], ids("AUX.1")),
        _pass(r + "W.1", [bar(0, g)]),
        _fail(r + "W.2", [bar(0, wmax + D)], ids("AUX.1")),
        _fail(r + "W.2", [bar(0, wmax + g)]),
        # The W.2 maximum itself is an even multiple of g (20 g), which
        # W.3 forbids: the boundary passes W.2 but trips W.3.
        _pass(r + "W.2", [bar(0, wmax)], ids("W.3")),
        _pass(r + "W.2", [bar(0, wmax - g)]),
        _fail(r + "W.3", [bar(0, 2 * g)]),
        _fail(r + "W.3", [bar(0, 4 * g)]),
        _pass(r + "W.3", [bar(0, 3 * g)]),
        _fail(r + "W.5", [bar(0, g, 0, 44 - D)]),
        _pass(r + "W.5", [bar(0, g, 0, 44)]),
        # Across the track: g apart on tracks 0 and 1. A neighbour one dbu
        # closer has an off-grid edge (see W.1); it is drawn 3 g wide so it
        # is not also an off-track minimum-width bar (AUX.2).
        _fail(r + "S.1", [bar(0, g), bar(2 * g - D, 3 * g)], ids("AUX.1")),
        _pass(r + "S.1", [bar(0, g), bar(2 * g, 3 * g)]),
        _pass(r + "S.1", [bar(0, g), bar(2 * g, g)]),
        # Along the track: tip to tip on one track.
        _fail(r + "S.2", [bar(0, g, 0, 100), bar(0, g, 140 - D, 240 - D)]),
        _pass(r + "S.2", [bar(0, g, 0, 100), bar(0, g, 140, 240)]),
        _fail(r + "AUX.1", [bar(g / 2, 3 * g)]),
        _pass(r + "AUX.1", [bar(g, 3 * g)]),
        # A minimum-width bar between tracks 0 and 1 (on the g grid, off the
        # 2 g track pitch).
        _fail(r + "AUX.2", [bar(g, g)]),
        _pass(r + "AUX.2", [bar(2 * g, g)]),
        _fail(
            r + "AUX.3",
            [poly(layer, (0, 0), (200, 0), (200, g), (44, g), (44, 3 * g), (0, 3 * g))],
        ),
        _pass(r + "AUX.3", [bar(0, g)]),
    ]
    return [_flip(x) for x in c] if direction == "v" else c


@dataclass(frozen=True)
class _Lele:
    """A V4-V7 via drawn in a frame where the upper metal runs along y and
    the lower metal along x (``flip`` transposes it to the real one)."""

    lower: str
    upper: str
    flip: bool
    along: float  # via size along the upper metal (y): its W.1 value
    across: float  # via size across the upper metal (x): the upper's width
    s: float  # S.1 / S.3
    g_lo: float  # lower metal grid / minimum width
    direction_known: bool  # W.1 / AUX.2 checkable (upper is not M8)


_LELE = {
    "V4": _Lele("M4", "M5", False, 24, 24, 33, 24, True),
    "V5": _Lele("M5", "M6", True, 24, 32, 33, 24, True),
    "V6": _Lele("M6", "M7", False, 32, 32, 45, 32, True),
    "V7": _Lele("M7", "M8", True, 32, 32, 45, 32, False),
}


def _odd_grid_top(g: float, need: float) -> float:
    """Smallest top edge >= ``need`` for a bar from ``-g`` that is an odd
    multiple of ``g`` tall (M4-M7 W.3) with both edges on the g grid."""
    top = -(-need // g) * g
    if ((top + g) / g) % 2 == 0:
        top += g
    return top


def _lele_upper_bar(
    p: _Lele, y0: float, y1: float, x0: float = 0, x1: float | None = None
) -> Box:
    """The upper metal over ``x0..x1`` (default: flush with the via) and
    ``y0..y1``. M8 (no track grid, 40 nm minimum width, 7520 nm^2 minimum
    area) is drawn 4 nm wider than the via on each side and 50 nm longer at
    both ends."""
    x1 = p.across if x1 is None else x1
    if p.upper == "M8":
        return box(p.upper, x0 - 4, y0 - 50, x1 + 4, y1 + 50)
    return box(p.upper, x0, y0, x1, y1)


def _lele_stack(
    via: str,
    p: _Lele,
    v: tuple[float, float, float, float] | None = None,
    lower: Box | None = None,
    upper: Box | None = None,
    no_lower: bool = False,
    no_upper: bool = False,
) -> list[Shape]:
    """One clean via (``v`` overrides its box) on its default lower and
    upper bars; ``lower`` / ``upper`` replace a bar, ``no_*`` omit it."""
    a, c = p.along, p.across
    shapes: list[Shape] = [box(via, *(v or (0, 0, c, a)))]
    if not no_lower:
        shapes.append(lower or box(p.lower, -50, 0, c + 50, a))
    if not no_upper:
        shapes.append(upper or _lele_upper_bar(p, -50, a + 50))
    return shapes


def _lele_size_cases(via: str, p: _Lele) -> list[Case]:
    """W.1 (exact size along the upper metal) and AUX.2 (exactly as wide as
    the upper metal), V4-V6 only."""
    a, c, g, lo, up = p.along, p.across, p.g_lo, p.lower, p.upper
    w1, aux2 = f"{via}.W.1", f"{via}.{up}.AUX.2"
    wide_lower = box(lo, -50, -g, c + 50, a + g)
    return [
        _fail(w1, _lele_stack(via, p, (0, 0, c, a - D))),
        _fail(w1, _lele_stack(via, p, (0, 0, c, a + D), lower=wide_lower)),
        _pass(w1, _lele_stack(via, p)),
        _fail(aux2, _lele_stack(via, p, (0, 0, c - D, a))),
        # A 3-track-wide upper metal with a 1-track via.
        _fail(aux2, _lele_stack(via, p, upper=box(up, 0, -50, 3 * c, a + 50))),
        _pass(aux2, _lele_stack(via, p)),
        _pass(
            aux2,
            _lele_stack(
                via,
                p,
                (0, 0, 3 * c, a),
                lower=box(lo, -50, 0, 3 * c + 50, a),
                upper=box(up, 0, -50, 3 * c, a + 50),
            ),
        ),
    ]


def _lele_corner_dy(p: _Lele) -> tuple[float, float, float]:
    """``(dx, dy_fail, dy_pass)`` for S.3. On M5-M7 the second via sits on
    the next track (dx = one track width) and s is not a lattice distance,
    so the fail / pass pair straddles it by one dbu in dy. Under M8 one
    wide bar holds both vias and (dx, dy) = (27, 36) is exactly 45 nm."""
    if p.upper == "M8":
        return 27.0, 36 - D, 36.0
    dx = p.across
    dy0 = math.sqrt(p.s * p.s - dx * dx)
    dy_fail, dy_pass = math.floor(dy0 / D) * D, math.ceil(dy0 / D) * D
    assert dy_fail < dy0 < dy_pass
    return dx, dy_fail, dy_pass


def _lele_spacing_cases(via: str, p: _Lele) -> list[Case]:
    a, c, s, g, lo = p.along, p.across, p.s, p.g_lo, p.lower

    # S.1: two vias stacked along one upper bar, `gap` apart, on one lower
    # bar wide enough (an odd number of grid units) for both.
    def pair(gap: float) -> list[Shape]:
        return [
            box(via, 0, 0, c, a),
            box(via, 0, a + gap, c, 2 * a + gap),
            box(lo, -50, -g, c + 50, _odd_grid_top(g, 2 * a + s)),
            _lele_upper_bar(p, -50, 2 * a + s + 50),
        ]

    # S.3: two vias diagonally apart with no projected overlap.
    dx, dy_fail, dy_pass = _lele_corner_dy(p)

    def corner(dy: float) -> list[Shape]:
        x1 = c + dx
        shapes: list[Shape] = [
            box(via, 0, 0, c, a),
            box(via, x1, a + dy, x1 + c, 2 * a + dy),
            box(lo, -50, -g, x1 + c + 50, _odd_grid_top(g, 2 * a + dy)),
        ]
        if p.upper == "M8":
            return [*shapes, _lele_upper_bar(p, -50, 2 * a + dy + 50, 0, x1 + c)]
        return [
            *shapes,
            _lele_upper_bar(p, -50, a + 50),
            _lele_upper_bar(p, a + dy - 50, 2 * a + dy + 50, x1, x1 + c),
        ]

    return [
        _fail(f"{via}.S.1", pair(s - D)),
        _pass(f"{via}.S.1", pair(s)),
        _fail(f"{via}.S.3", corner(dy_fail)),
        _pass(f"{via}.S.3", corner(dy_pass)),
    ]


def _lele_enclosure_cases(via: str, p: _Lele) -> list[Case]:
    a, c, g, lo, up = p.along, p.across, p.g_lo, p.lower, p.upper
    en1, en2 = f"{via}.{lo}.EN.1", f"{via}.{up}.EN.2"

    # Enclosure by the lower metal, 11 nm past the via ends along x (flush
    # across).
    def lower_capped(e0: float, e1: float) -> list[Shape]:
        return _lele_stack(via, p, lower=box(lo, -e0, 0, c + e1, a))

    def upper_box(x0: float, y0: float, x1: float, y1: float) -> list[Shape]:
        return _lele_stack(via, p, upper=box(up, x0, y0, x1, y1))

    cs = [
        _fail(en1, lower_capped(11 - D, 11 - D)),
        _fail(en1, lower_capped(11, 11 - D)),
        _pass(en1, lower_capped(11, 11)),
        # Short along the bar, but 3 grid units tall: the other pair of
        # sides meets the minimum.
        _pass(
            en1, _lele_stack(via, p, lower=box(lo, -(11 - D), -g, c + 11 - D, a + g))
        ),
    ]
    # Enclosure by the upper metal, 11 nm past the via ends along y.
    if up == "M8":
        # M8 needs 7520 nm^2: one end stays long; across is 4 nm.
        return cs + [
            _fail(en2, upper_box(-4, -(11 - D), c + 4, a + 200)),
            _pass(en2, upper_box(-4, -11, c + 4, a + 200)),
            _pass(en2, upper_box(-11, -4, c + 11, a + 200)),
        ]
    return cs + [
        _fail(en2, upper_box(0, -(11 - D), c, a + 11 - D)),
        _fail(en2, upper_box(0, -11, c, a + 11 - D)),
        _pass(en2, upper_box(0, -11, c, a + 11)),
    ]


def _lele_via_one(via: str, p: _Lele) -> list[Case]:
    aux1 = f"{via}.AUX.1"
    cs = [
        *(_lele_size_cases(via, p) if p.direction_known else []),
        *_lele_spacing_cases(via, p),
        *_lele_enclosure_cases(via, p),
        _fail(aux1, _lele_stack(via, p, no_lower=True)),
        _fail(aux1, _lele_stack(via, p, no_upper=True)),
        _pass(aux1, _lele_stack(via, p)),
    ]
    return [_flip(x) for x in cs] if p.flip else cs


def _lele_via_cases() -> list[Case]:
    return [c for via, p in _LELE.items() for c in _lele_via_one(via, p)]


def _top_metal_cases(layer: str) -> list[Case]:
    """Tables 3.18.1-3.18.2, identical for M8 and M9."""
    r = f"{layer}."

    def b(x1: float, y1: float, x2: float, y2: float) -> Box:
        return box(layer, x1, y1, x2, y2)

    def neck(h: float) -> Poly:
        y0, y1 = 55, 55 + h
        return poly(
            layer,
            (0, 0), (0, 150), (150, 150), (150, y1), (200, y1), (200, 150),
            (350, 150), (350, 0), (200, 0), (200, y0), (150, y0), (150, 0),
        )  # fmt: skip

    c: list[Case] = [
        # Two 150 nm squares joined by a 50 nm long neck `h` tall: a
        # rectangle under 40 nm wide would also have an edge under 40 nm
        # (L.1); the neck's edges are all >= 50 nm.
        _fail(r + "W.1", [neck(40 - D)]),
        _pass(r + "W.1", [neck(40)]),
        _fail(r + "W.2", [b(0, 0, 60 - D, 400)]),
        _pass(r + "W.2", [b(0, 0, 60, 400)]),
        # One dbu shorter than 400 nm: the 60 nm minimum does not apply.
        _pass(r + "W.2", [b(0, 0, 60 - D, 400 - D)]),
        _fail(r + "W.3", [b(0, 0, 80 - D, 1200)]),
        _pass(r + "W.3", [b(0, 0, 80, 1200)]),
        _fail(r + "W.4", [b(0, 0, 120 - D, 1800)]),
        _pass(r + "W.4", [b(0, 0, 120, 1800)]),
        _fail(r + "W.5", [b(0, 0, 2000 + D, 2000 + D)]),
        _pass(r + "W.5", [b(0, 0, 2000, 2000)]),
    ]
    for gap, mk in ((40 - D, _fail), (40, _pass)):
        c.append(mk(r + "S.1", [b(0, 0, 40, 300), b(40 + gap, 0, 80 + gap, 300)]))
    for gap, mk in ((43 - D, _fail), (43, _pass)):
        c.append(mk(r + "S.2", [b(0, 0, 40, 300), b(-100, 300 + gap, 140, 340 + gap)]))
    for gap, mk in ((46 - D, _fail), (46, _pass)):
        c.append(mk(r + "S.3", [b(0, 0, 40, 300), b(0, 300 + gap, 40, 600 + gap)]))
    c += [
        _fail(r + "A.1", [b(0, 0, 40, 188 - D)]),
        _pass(r + "A.1", [b(0, 0, 40, 188)]),
    ]

    # A 200 x 100 nm block with a 100 nm wide step `j` tall on its left
    # half: the step's right edge is j long, every width stays >= 100 nm.
    def jog(j: float) -> Poly:
        return poly(
            layer,
            (0, 0),
            (200, 0),
            (200, 100),
            (100, 100),
            (100, 100 + j),
            (0, 100 + j),
        )

    c += [_fail(r + "L.1", [jog(40 - D)]), _pass(r + "L.1", [jog(40)])]
    return c


#: V8 / V9 -> (metal below, metal above or None: PAD is not read).
_TOP_VIAS: dict[str, tuple[str, str | None]] = {"V8": ("M8", "M9"), "V9": ("M9", None)}


def _top_via_one(via: str, lo: str, up: str | None) -> list[Case]:
    out: list[Case] = []
    metal_default = (-100.0, -20.0, 140.0, 60.0)

    def stack(
        v: tuple[float, float, float, float] = (0, 0, 40, 40),
        *extra: tuple[float, float, float, float],
        metal: tuple[float, float, float, float] = metal_default,
        lower: tuple[float, float, float, float] | None = None,
        upper: tuple[float, float, float, float] | None = None,
        no_lower: bool = False,
        no_upper: bool = False,
    ) -> list[Shape]:
        shapes: list[Shape] = [box(via, *v), *(box(via, *e) for e in extra)]
        if not no_lower:
            shapes.append(box(lo, *(lower or metal)))
        if up is not None and not no_upper:
            shapes.append(box(up, *(upper or metal)))
        return shapes

    big = (-100.0, -100.0, 220.0, 220.0)
    w1 = f"{via}.W.1"
    out += [
        _fail(w1, stack((0, 0, 40, 40 - D))),
        _fail(w1, stack((0, 0, 40, 40 + D))),
        _fail(w1, stack((0, 0, 120, 120), metal=big)),
        _pass(w1, stack()),
        _pass(w1, stack((0, 0, 40, 120), metal=big)),
        _pass(w1, stack((0, 0, 120, 40), metal=big)),
    ]
    for gap, mk in ((57 - D, _fail), (57, _pass)):
        out.append(
            mk(
                f"{via}.S.1",
                stack(
                    (0, 0, 40, 40),
                    (40 + gap, 0, 80 + gap, 40),
                    metal=(-100, -20, 240, 60),
                ),
            )
        )
    # Diagonal, 40 nm across: 57 nm is not a lattice distance, so the
    # fail / pass pair straddles it by one dbu in dy.
    for dy, mk in ((40.5, _fail), (40.75, _pass)):
        out.append(
            mk(
                f"{via}.S.2",
                stack(
                    (0, 0, 40, 40),
                    (80, 40 + dy, 120, 80 + dy),
                    metal=(-100, -20, 220, 160),
                ),
            )
        )
    # Enclosure 20 nm, past the via's left end; the right end is long
    # and the metal is 10 nm past the via across.
    en1 = f"{via}.{lo}.EN.1"
    out += [
        _fail(en1, stack(lower=(-(20 - D), -10, 300, 50))),
        _pass(en1, stack(lower=(-20, -10, 300, 50))),
    ]
    if up is not None:
        en2 = f"{via}.{up}.EN.2"
        out += [
            _fail(en2, stack(upper=(-(20 - D), -10, 300, 50))),
            _pass(en2, stack(upper=(-20, -10, 300, 50))),
        ]
        aux1 = f"{via}.AUX.1"
        out += [
            _fail(aux1, stack(no_lower=True)),
            _fail(aux1, stack(no_upper=True)),
            _pass(aux1, stack()),
        ]
    return out


def _top_via_cases() -> list[Case]:
    return [c for via, (lo, up) in _TOP_VIAS.items() for c in _top_via_one(via, lo, up)]


def _geometry_cases() -> list[Case]:
    chamfered = poly("WELL", (0, 0), (0, 300), (240, 300), (300, 240), (300, 0))
    # The top BEOL layer, chamfered so that it meets every M9 width, area
    # and edge-length rule: only the geometry check can see it.
    beol = poly("M9", (0, 0), (0, 300), (240, 300), (300, 240), (300, 0))
    return [
        _fail("GEOMETRY.NONORTHOGONAL", [chamfered]),
        _pass("GEOMETRY.NONORTHOGONAL", [box("WELL", 0, 0, 300, 300)]),
        _fail("GEOMETRY.NONORTHOGONAL", [beol]),
        _pass("GEOMETRY.NONORTHOGONAL", [box("M9", 0, 0, 300, 300)]),
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
        *_metal_cases("M1"),
        *_metal_cases("M2"),
        *_metal_cases("M3"),
        *_via_cases(),
        *_sadp_metal_cases("M4"),
        *_sadp_metal_cases("M5"),
        *_sadp_metal_cases("M6"),
        *_sadp_metal_cases("M7"),
        *_lele_via_cases(),
        *_top_metal_cases("M8"),
        *_top_metal_cases("M9"),
        *_top_via_cases(),
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
