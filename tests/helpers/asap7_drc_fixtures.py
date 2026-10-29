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
    "PAD": 96,
    "SDT": 88,
    "SLVT": 97,
    "LVT": 98,
    "SRAMDRC": 99,
    "SRAMVT": 110,
    # Assumed stream number: the DRM / asap7.lyp have no thick-oxide marker (#4465).
    "THICKOX": 111,
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
        box("ACTIVE", 10, 30, 99, 111),
        box("NSELECT", -36, 3, 145, 138),
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


def _active_w2_cases() -> list[Case]:
    """ACTIVE.W.2: every local vertical width is N x 27 nm, N a positive
    integer. One database unit either side of one and of several allowed
    multiples; stepped, holed and merged polygons are measured locally, never
    by bounding-box height; horizontal widths are not constrained."""
    c: list[Case] = []
    for h in (27 + D, 54 - D, 54 + D, 81 - D, 81 + D, 135 - D):
        c.append(_fail("ACTIVE.W.2", active_in("NSELECT", 0, 0, 70, h)))
    # Allowed multiples: one (the minimum), two, three and five. The 70 nm
    # width is not a multiple of 27 nm: only the vertical direction counts.
    for h in (27, 54, 81, 135):
        c.append(_pass("ACTIVE.W.2", active_in("NSELECT", 0, 0, 70, h)))
    # Stepped: the 81 nm bounding box is a multiple but the 60 nm right-hand
    # step is not (fail); a 94.5 nm bounding box is not a multiple but both
    # steps are 81 nm tall (pass).
    short_step = poly(
        "ACTIVE", (0, 0), (0, 81), (70, 81), (70, 60), (140, 60), (140, 0)
    )
    offset_step = poly(
        "ACTIVE",
        (0, 0),
        (0, 81),
        (70, 81),
        (70, 94.5),
        (140, 94.5),
        (140, 13.5),
        (70, 13.5),
        (70, 0),
    )
    for rule_case, step, sel in (
        (_fail, short_step, box("NSELECT", -46, -27, 186, 108)),
        (_pass, offset_step, box("NSELECT", -46, -27, 186, 121.5)),
    ):
        c.append(rule_case("ACTIVE.W.2", [step, sel]))
    # Holed: the 40 nm bar under the hole is not a multiple, the 81 nm
    # columns either side are.
    sel = box("NSELECT", -46, -27, 246, 162)
    c.append(
        _fail("ACTIVE.W.2", [ring("ACTIVE", (0, 0, 200, 135), (84, 40, 150, 108)), sel])
    )
    c.append(
        _pass("ACTIVE.W.2", [ring("ACTIVE", (0, 0, 200, 135), (84, 54, 150, 108)), sel])
    )
    # Merged: two abutting boxes of 40 and 41 nm are one 81 nm polygon.
    c.append(
        _pass(
            "ACTIVE.W.2",
            [
                box("ACTIVE", 0, 0, 70, 40),
                box("ACTIVE", 0, 40, 70, 81),
                box("NSELECT", -46, -27, 116, 108),
            ],
        )
    )
    return c


def _active_cases() -> list[Case]:
    c: list[Case] = []
    c += [
        # Pulling ACTIVE's top edge in by one database unit leaves 9.75 nm past
        # the top fin; no ACTIVE whose height stays a multiple of 27 nm both
        # fails here and keeps the gate/GCUT rules clean, so ACTIVE.W.2 (and
        # SDT.W.3 for the SDT drawn to ACTIVE's height) trip with it.
        _fail(
            "ACTIVE.FIN.EX.1",
            device(ay1=108 - D),
            also=["ACTIVE.W.2", "SDT.W.3"],
        ),
        _pass("ACTIVE.FIN.EX.1", device()),
    ]
    # A height below 27 nm is no positive multiple of 27 nm either.
    c.append(
        _fail("ACTIVE.W.1", active_in("NSELECT", 0, 0, 70, 27 - D), also=["ACTIVE.W.2"])
    )
    c.append(_pass("ACTIVE.W.1", active_in("NSELECT", 0, 0, 70, 27)))
    c += _active_w2_cases()
    c.append(_fail("ACTIVE.W.3", active_in("NSELECT", 0, 0, 16 - D, 81, margin_h=50)))
    c.append(_pass("ACTIVE.W.3", active_in("NSELECT", 0, 0, 16, 81, margin_h=50)))
    for gap, mk in ((27 - D, _fail), (27, _pass)):
        sel = box("NSELECT", -46, -27, 116, 108 + gap + 27)
        c.append(
            mk(
                "ACTIVE.S.1",
                [
                    box("ACTIVE", 0, 0, 70, 54),
                    box("ACTIVE", 0, 54 + gap, 70, 108 + gap),
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
    ring_sel = box("NSELECT", -46, -27, 246, 108)
    c.append(
        _fail(
            "ACTIVE.A.1B",
            [ring("ACTIVE", (0, 0, 200, 81), (84, 27, 116 - D, 54)), ring_sel],
            also=["ACTIVE.S.2B"],
        )
    )
    c.append(
        _pass(
            "ACTIVE.A.1B",
            [ring("ACTIVE", (0, 0, 200, 81), (84, 27, 116, 54)), ring_sel],
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


def _tk_device(
    *,
    gate_w: float = 20,
    active_h: float = 81,
    **kw: float,
) -> list[Shape]:
    """A THICKOX-marked :func:`device` (marker 46/27 nm past ACTIVE) whose
    channel GATE is ``gate_w`` wide and whose ACTIVE is ``active_h`` tall."""
    shapes = device(vt="THICKOX", ay1=27 + active_h, **kw)
    if gate_w != 20:
        shapes = [
            box("GATE", 71, 10, 71 + gate_w, 125)
            if s == box("GATE", 71, 10, 91, 125)
            else s
            for s in shapes
        ]
    return shapes


def _thickox_cases() -> list[Case]:
    """THICKOX.* (#4465): predictive rules, every threshold assumed."""
    # Every marked device here draws the core 20 nm gate (GATE.W.1 requires
    # it), so it is also under the assumed 30 nm THICKOX.GATE.L minimum.
    lg = ["THICKOX.GATE.L"]
    # A 30 nm channel gate crowds its neighbours and the SDT; a 53.75 nm
    # ACTIVE is off the 27 nm height step and short of the fins.
    gw = ["GATE.S.2", "GATE.ACTIVE.EX.2", "SDT.GATE.AUX.1"]
    aw = ["ACTIVE.W.2", "ACTIVE.FIN.EX.1", "SDT.W.3"]
    core = device()
    return [
        _fail("THICKOX.ACTIVE.EN", device(vt="THICKOX", vt_h=27 - D, vt_v=27 - D), lg),
        _pass("THICKOX.ACTIVE.EN", device(vt="THICKOX", vt_h=27, vt_v=27), lg),
        # marker edge coincident with the ACTIVE edge: zero enclosure
        _fail("THICKOX.ACTIVE.EN", device(vt="THICKOX", vt_h=0, vt_v=27), lg),
        _fail(
            "THICKOX.S",
            [box("THICKOX", 0, 0, 120, 60), box("THICKOX", 174 - D, 0, 294, 60)],
        ),
        _pass(
            "THICKOX.S",
            [box("THICKOX", 0, 0, 120, 60), box("THICKOX", 174, 0, 294, 60)],
        ),
        # abutting and overlapping markers merge into one
        _pass(
            "THICKOX.S",
            [box("THICKOX", 0, 0, 120, 60), box("THICKOX", 120, 0, 240, 60)],
        ),
        _pass(
            "THICKOX.S",
            [box("THICKOX", 0, 0, 120, 60), box("THICKOX", 100, 0, 220, 60)],
        ),
        _fail("THICKOX.GATE.L", _tk_device(gate_w=30 - D), [*gw, "GATE.W.1"]),
        # the conforming 30 nm marked gate is itself an exact-20 nm GATE.W.1 miss
        _pass("THICKOX.GATE.L", _tk_device(gate_w=30), [*gw, "GATE.W.1"]),
        # a core device (no marker) keeps its 20 nm gate
        _pass("THICKOX.GATE.L", core),
        _fail("THICKOX.ACTIVE.W", _tk_device(active_h=54 - D), [*lg, *aw]),
        _pass("THICKOX.ACTIVE.W", _tk_device(active_h=54), lg),
        # a core device keeps a narrow (27 nm) channel
        _pass("THICKOX.ACTIVE.W", device(ay1=54)),
    ]


def _sram_active(
    sel: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    h: float = 46,
    v: float = 27,
    marker: bool = True,
) -> list[Shape]:
    """An ACTIVE box under ``sel`` (``h`` / ``v`` nm of horizontal / vertical
    enclosure) plus, when ``marker``, an SRAMDRC box that overlaps the ACTIVE
    by half its width. Every other rule sees the same geometry either way, so
    the with/without pair isolates the SRAMDRC scoping."""
    shapes: list[Shape] = [box("ACTIVE", x1, y1, x2, y2)]
    if sel == "NSELECT" or sel == "PSELECT":
        shapes.append(box(sel, x1 - h, y1 - v, x2 + h, y2 + v))
    else:
        # ACTIVE.AUX.1 wants ACTIVE under NSELECT or PSELECT, so the VT layers
        # ride on a loosely enclosing NSELECT.
        shapes.append(box("NSELECT", x1 - 46, y1 - 27, x2 + 46, y2 + 27))
        shapes.append(box(sel, x1 - h, y1 - v, x2 + h, y2 + v))
    if marker:
        shapes.append(box("SRAMDRC", (x1 + x2) / 2, y1 - 10, x2 + 10, y2 + 10))
    return shapes


def _sram_cases() -> list[Case]:
    """SRAMDRC-scoped ACTIVE rules (DRM 1.2.2 note 7, Tables 3.5.1, 3.5.2 and
    3.7.1). Each threshold pair is paired with a control that draws the same
    geometry without SRAMDRC, where the non-SRAM rule applies instead."""
    c: list[Case] = []
    active = (0, 0, 70, 81)
    for gap, mk in ((13.5 - D, _fail), (13.5, _pass)):
        well = box("WELL", 70 + gap, 0, 300, 81)
        c.append(mk("SRAM.ACTIVE.WELL.S.5", [*_sram_active("NSELECT", *active), well]))
    # Outside SRAMDRC the same 13.5 nm gap is the 27 nm ACTIVE.WELL.S.4 case.
    c.append(
        _fail(
            "ACTIVE.WELL.S.4",
            [
                *_sram_active("NSELECT", *active, marker=False),
                box("WELL", 83.5, 0, 300, 81),
            ],
        )
    )
    for m, mk in ((13.5 - D, _fail), (13.5, _pass)):
        well = box("WELL", -m, -m, 130, 141)
        c.append(mk("SRAM.ACTIVE.WELL.EN.2", [*_sram_active("PSELECT", *active), well]))
    c.append(
        _fail(
            "ACTIVE.WELL.EN.1",
            [
                *_sram_active("PSELECT", *active, marker=False),
                box("WELL", -13.5, -13.5, 130, 141),
            ],
        )
    )

    # Area: the smallest legal ACTIVE (16 x 27 nm, ACTIVE.W.3 x ACTIVE.W.1) is
    # exactly 432 nm^2, so anything smaller also breaks a width rule.
    thin = (0, 0, 16, 27)
    fail_thin = (0, 0, 16, 27 - D)
    kw = {"h": 52, "v": 27}
    c.append(
        _fail(
            "SRAM.ACTIVE.A.2A",
            _sram_active("NSELECT", *fail_thin, **kw),
            also=["ACTIVE.W.1", "ACTIVE.W.2"],
        )
    )
    c.append(_pass("SRAM.ACTIVE.A.2A", _sram_active("NSELECT", *thin, **kw)))
    c.append(_fail("ACTIVE.A.1A", _sram_active("NSELECT", *thin, marker=False, **kw)))
    outer = (0, 0, 200, 120)
    for hole, mk, also in (
        ((84, 46, 100, 73 - D), _fail, ["ACTIVE.S.2B", "ACTIVE.S.1", "ACTIVE.W.2"]),
        ((84, 46, 100, 73), _pass, ["ACTIVE.S.2B", "ACTIVE.W.2"]),
    ):
        shapes = [ring("ACTIVE", outer, hole), box("NSELECT", -46, -27, 246, 147)]
        shapes.append(box("SRAMDRC", 0, 0, 20, 20))
        c.append(mk("SRAM.ACTIVE.A.2B", shapes, also=also))
    c.append(
        _fail(
            "ACTIVE.A.1B",
            [
                ring("ACTIVE", outer, (84, 46, 100, 73)),
                box("NSELECT", -46, -27, 246, 147),
            ],
            also=["ACTIVE.S.2B", "ACTIVE.W.2"],
        )
    )

    # AUX.2: ACTIVE sharing no area with SRAMDRC may not touch it. An edge
    # abutment or a single shared corner is a touch; one database unit of
    # clearance, or real overlap, is not.
    base = active_in("NSELECT", 0, 0, 70, 81)
    abut = [*base, box("SRAMDRC", 70, 0, 150, 81)]
    # Touching makes the polygon "interact", so it is also held to the SRAM
    # values -- which it meets with the full 46 / 27 nm enclosure.
    c.append(_fail("SRAM.ACTIVE.AUX.2", abut))
    c.append(_fail("SRAM.ACTIVE.AUX.2", [*base, box("SRAMDRC", 70, 81, 150, 160)]))
    c.append(_pass("SRAM.ACTIVE.AUX.2", [*base, box("SRAMDRC", 70 + D, 0, 150, 81)]))
    c.append(_pass("SRAM.ACTIVE.AUX.2", [*base, box("SRAMDRC", 35, 0, 150, 81)]))

    # Select enclosure of SRAM ACTIVE: 13.5 nm each way, for the NSELECT rules
    # and the section 3.7 mirrors on the other four layers. 90 x 81 nm ACTIVE
    # keeps the select above its own 108 x 54 nm minimum.
    wide = (0, 0, 90, 81)
    for name in ("NSELECT", "PSELECT", "SLVT", "LVT", "SRAMVT"):
        c.append(
            _fail(
                f"SRAM.{name}.ACTIVE.EN.3", _sram_active(name, *wide, h=13.5 - D, v=27)
            )
        )
        c.append(
            _pass(f"SRAM.{name}.ACTIVE.EN.3", _sram_active(name, *wide, h=13.5, v=27))
        )
        c.append(
            _fail(
                f"SRAM.{name}.ACTIVE.EN.4", _sram_active(name, *wide, h=46, v=13.5 - D)
            )
        )
        c.append(
            _pass(f"SRAM.{name}.ACTIVE.EN.4", _sram_active(name, *wide, h=46, v=13.5))
        )
        # Without SRAMDRC the same 13.5 nm enclosure is the 46 / 27 nm rule.
        c.append(
            _fail(
                f"{name}.ACTIVE.EN.1",
                _sram_active(name, *wide, h=13.5, v=27, marker=False),
            )
        )
        c.append(
            _fail(
                f"{name}.ACTIVE.EN.2",
                _sram_active(name, *wide, h=46, v=13.5, marker=False),
            )
        )
    return c


def _sdt_w3_cases() -> list[Case]:
    """SDT.W.3: like ACTIVE.W.2 for SDT (outside SRAMDRC). SDT horizontal
    edges lie on ACTIVE's (SDT.ACTIVE.AUX.2) and SDT sits inside LISD
    (SDT.LISD.AUX.4), so each case draws all three at the same height, and
    an off-multiple SDT is an off-multiple ACTIVE too (ACTIVE.W.2)."""
    c: list[Case] = []

    def column(h: float, *, sram: bool = False) -> list[Shape]:
        shapes: list[Shape] = [
            *active_in("NSELECT", 0, 0, 70, h),
            box("SDT", 10, 0, 34, h),
            box("LISD", 10, 0, 34, h),
        ]
        if sram:
            shapes.append(box("SRAMDRC", -10, -10, 80, h + 10))
        return shapes

    for h in (27 + D, 54 - D, 54 + D, 81 - D, 81 + D):
        c.append(_fail("SDT.W.3", column(h), also=["ACTIVE.W.2"]))
    for h in (27, 54, 81, 135):
        c.append(_pass("SDT.W.3", column(h)))
    # The rule excludes SDT interacting with SRAMDRC; ACTIVE is not excluded.
    c.append(_pass("SDT.W.3", column(81 - D, sram=True), also=["ACTIVE.W.2"]))
    # Merged: two abutting SDT boxes of 40 and 41 nm are one 81 nm polygon.
    merged = [
        *active_in("NSELECT", 0, 0, 70, 81),
        box("SDT", 10, 0, 34, 40),
        box("SDT", 10, 40, 34, 81),
        box("LISD", 10, 0, 34, 81),
    ]
    c.append(_pass("SDT.W.3", merged))
    # Stepped: an 81 nm bounding box is a multiple, the 60 nm step is not.
    # The step edge is inside ACTIVE, so SDT.ACTIVE.AUX.2 is expected too.
    step = [
        *active_in("NSELECT", 0, 0, 70, 81),
        poly("SDT", (10, 0), (10, 81), (34, 81), (34, 60), (58, 60), (58, 0)),
        box("LISD", 10, 0, 58, 81),
    ]
    c.append(_fail("SDT.W.3", step, also=["SDT.ACTIVE.AUX.2"]))
    return c


def _sdt_cases() -> list[Case]:
    c: list[Case] = []
    c += [_fail("SDT.W.1", device(sdt1=(42, 66 - D))), _pass("SDT.W.1", device())]
    # SDT edges sit on ACTIVE edges (SDT.ACTIVE.AUX.2) and SDT sits inside
    # LISD (SDT.LISD.AUX.4), so a short SDT is also a short ACTIVE, a short
    # SDT/ACTIVE overlap and a short SDT/LISD overlap -- and 27 nm less one
    # database unit is no whole multiple of 27 nm (ACTIVE.W.2, SDT.W.3).
    short = [
        "SDT.W.2",
        "SDT.ACTIVE.OV.1",
        "SDT.LISD.OV.2",
        "ACTIVE.W.1",
        "ACTIVE.W.2",
        "SDT.W.3",
    ]

    def flat_sd(h: float) -> list[Shape]:
        return [
            *active_in("NSELECT", 0, 0, 70, h),
            box("SDT", 10, 0, 34, h),
            box("LISD", 10, 0, 34, 60),
        ]

    for rule in short[:3]:
        c.append(_fail(rule, flat_sd(27 - D), also=[r for r in short if r != rule]))
        c.append(_pass(rule, flat_sd(27)))
    c += _sdt_w3_cases()
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
    aux2 += [box("SDT", 42, 27 + D, 66, 108 + D), box("LISD", 42, 27 + D, 66, 108 + D)]
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


def _sram_sdt_cases() -> list[Case]:
    """SRAM.SDT.W.4 / ACTIVE.OV.3 / LISD.OV.4 (17 nm, SDT interacting with
    SRAMDRC), each with the same geometry drawn without the marker (which the
    unmarked 27 nm rules then judge) and with the marker only touching it."""
    c: list[Case] = []
    marker = box("SRAMDRC", -60, -40, 130, 120)

    def sd(
        sdt: tuple[float, float],
        active: tuple[float, float] = (0, 81),
        lisd: tuple[float, float] = (0, 60),
        mark: Box | None = marker,
    ) -> list[Shape]:
        shapes = [
            *active_in("NSELECT", 0, active[0], 70, active[1]),
            box("SDT", 10, sdt[0], 34, sdt[1]),
            box("LISD", 10, lisd[0], 34, lisd[1]),
        ]
        return shapes + ([mark] if mark else [])

    # A SDT that is short is also a short overlap with the ACTIVE and LISD it
    # sits on (SDT.LISD.AUX.4 forces the LISD to cover it).
    # Unmarked, the 17 - D nm SDT is also no whole multiple of 27 nm (SDT.W.3).
    unmarked = [
        "SDT.W.2",
        "SDT.W.3",
        "SDT.ACTIVE.OV.1",
        "SDT.LISD.OV.2",
        "SDT.ACTIVE.AUX.2",
    ]
    all3 = ["SRAM.SDT.W.4", "SRAM.SDT.ACTIVE.OV.3", "SRAM.SDT.LISD.OV.4"]
    short = (0, 17 - D)
    c.append(_pass("SRAM.SDT.W.4", sd((0, 17))))
    c.append(_fail("SRAM.SDT.W.4", sd(short), also=all3[1:]))
    # Marker edge: touching SDT's top edge is interacting; one dbu clear of it
    # is not, and the unmarked rules apply instead.
    c.append(
        _fail(
            "SRAM.SDT.W.4",
            sd(short, mark=box("SRAMDRC", 0, 17 - D, 60, 120)),
            also=all3[1:],
        )
    )
    c.append(
        _pass(
            "SRAM.SDT.W.4",
            sd(short, mark=box("SRAMDRC", 0, 17, 60, 120)),
            also=unmarked,
        )
    )
    c.append(_pass("SRAM.SDT.W.4", sd(short, mark=None), also=unmarked))
    # ACTIVE overlaps only the upper 17 - D nm of a 40 nm SDT. The trimmed
    # ACTIVE is no whole multiple of 27 nm, and ACTIVE.W.2 ignores SRAMDRC.
    c.append(
        _pass(
            "SRAM.SDT.ACTIVE.OV.3",
            sd((0, 40), active=(23, 81)),
            also=["ACTIVE.W.2"],
        )
    )
    c.append(
        _fail(
            "SRAM.SDT.ACTIVE.OV.3",
            sd((0, 40), active=(23 + D, 81)),
            also=["ACTIVE.W.2"],
        )
    )
    c.append(
        _pass(
            "SRAM.SDT.ACTIVE.OV.3",
            sd((0, 40), active=(23 + D, 81), mark=None),
            also=["SDT.ACTIVE.OV.1", "SDT.ACTIVE.AUX.2", "SDT.W.3", "ACTIVE.W.2"],
        )
    )
    # LISD overlaps only the upper part of the SDT, so SDT.LISD.AUX.4 (SDT
    # inside LISD) trips with the overlap rule: the two cannot be separated.
    c.append(_pass("SRAM.SDT.LISD.OV.4", sd((0, 17))))
    c.append(
        _fail(
            "SRAM.SDT.LISD.OV.4",
            sd((0, 40), lisd=(23 + D, 60)),
            also=["SDT.LISD.AUX.4"],
        )
    )
    c.append(
        _pass(
            "SRAM.SDT.LISD.OV.4",
            sd((0, 40), lisd=(23 + D, 60), mark=None),
            also=[
                "SDT.LISD.OV.2",
                "SDT.LISD.AUX.4",
                "SDT.ACTIVE.AUX.2",
                "SDT.W.3",
            ],
        )
    )
    return c


def _sram_lisd_cases() -> list[Case]:
    c: list[Case] = []

    def tips(w: float, gap: float, mark: Box | None) -> list[Shape]:
        shapes = [box("LISD", 0, 0, w, 100), box("LISD", 0, 100 + gap, w, 200 + gap)]
        return shapes + ([mark] if mark else [])

    marker = box("SRAMDRC", -50, -50, 100, 260)
    c.append(_pass("SRAM.LISD.S.4", tips(24, 27, marker)))
    c.append(_fail("SRAM.LISD.S.4", tips(24, 27 - D, marker)))
    # 36 nm is still a tip edge; 36.25 nm is a side edge (LISD.S.1's 18 nm).
    c.append(_fail("SRAM.LISD.S.4", tips(36, 27 - D, marker)))
    c.append(_pass("SRAM.LISD.S.4", tips(36 + D, 18, marker)))
    # Outside SRAMDRC the same tips are LISD.S.3's.
    c.append(_pass("SRAM.LISD.S.4", tips(24, 27 - D, None), also=["LISD.S.3"]))
    # Only the first LISD polygon is in scope: neither this rule nor LISD.S.3
    # (which wants both polygons clear of SRAMDRC) judges the pair.
    c.append(
        _pass("SRAM.LISD.S.4", tips(24, 27 - D, box("SRAMDRC", -50, -50, 100, 110)))
    )
    # LISD.AUX.1: a LISD not overlapping SRAMDRC may not touch it.
    bar = box("LISD", 0, 0, 24, 100)
    c.append(_fail("SRAM.LISD.AUX.1", [bar, box("SRAMDRC", 24, 0, 150, 100)]))
    c.append(_fail("SRAM.LISD.AUX.1", [bar, box("SRAMDRC", 24, 100, 150, 200)]))
    c.append(_pass("SRAM.LISD.AUX.1", [bar, box("SRAMDRC", 24 + D, 0, 150, 100)]))
    c.append(_pass("SRAM.LISD.AUX.1", [bar, box("SRAMDRC", 10, 0, 150, 100)]))
    c.append(_pass("SRAM.LISD.AUX.1", [bar, box("SRAMDRC", -10, -10, 40, 110)]))
    return c


def _sram_lig_cases() -> list[Case]:
    c: list[Case] = []
    marker = box("SRAMDRC", -30, 30, 100, 110)

    def over(
        x: tuple[float, float], y: tuple[float, float], mark: Box | None = marker
    ) -> list[Shape]:
        shapes = [*gate_pair(), box("LIG", x[0], y[0], x[1], y[1])]
        return shapes + ([mark] if mark else [])

    # LIG across both gates (20 nm each): overlap area is 20 x (height past
    # the gate end at y 100) per gate. 12 nm tall = 240 nm^2.
    thin = (88 + D, 104 + D)
    c.append(_pass("SRAM.LIG.GATE.A.4", over((-17, 91), (88, 104))))
    c.append(_fail("SRAM.LIG.GATE.A.4", over((-17, 91), thin)))
    c.append(
        _pass("SRAM.LIG.GATE.A.4", over((-17, 91), thin, None), also=["LIG.GATE.A.3"])
    )
    # Marker touching the LIG's top edge only: still interacting, but the LIG
    # is then a SRAM.LIG.AUX.2 touch as well.
    c.append(
        _fail(
            "SRAM.LIG.GATE.A.4",
            over((-17, 91), thin, box("SRAMDRC", -30, 104 + D, 100, 140)),
            also=["SRAM.LIG.AUX.2"],
        )
    )
    # Horizontal overlap with the first gate only: 15 nm passes, 14.75 fails
    # even though the area (14.75 x 20 = 295 nm^2) clears SRAM.LIG.GATE.A.4.
    c.append(_pass("SRAM.LIG.GATE.OV.2", over((-17, 15), (40, 60))))
    c.append(_fail("SRAM.LIG.GATE.OV.2", over((-17, 15 - D), (40, 60))))
    c.append(
        _pass(
            "SRAM.LIG.GATE.OV.2",
            over((-17, 15 - D), (40, 60), None),
            also=["LIG.GATE.A.3", "LIG.GATE.AUX.1"],
        )
    )
    # LIG.AUX.2: a LIG not overlapping SRAMDRC may not touch it.
    bar = box("LIG", 0, 0, 16, 100)
    c.append(_fail("SRAM.LIG.AUX.2", [bar, box("SRAMDRC", 16, 0, 150, 100)]))
    c.append(_fail("SRAM.LIG.AUX.2", [bar, box("SRAMDRC", 16, 100, 150, 200)]))
    c.append(_pass("SRAM.LIG.AUX.2", [bar, box("SRAMDRC", 16 + D, 0, 150, 100)]))
    c.append(_pass("SRAM.LIG.AUX.2", [bar, box("SRAMDRC", 8, 0, 150, 100)]))
    c.append(_pass("SRAM.LIG.AUX.2", [bar, box("SRAMDRC", -10, -10, 30, 110)]))
    return c


# --------------------------------------------------------------------------
# Net-aware spacing (#3084): ACTIVE.S.2A, LIG.LISD.S.6/S.7, LIG.SDT.S.8
# --------------------------------------------------------------------------
#
# Every pair below is drawn twice: once with nothing joining the two shapes
# (different nets) and once with a conductor chain that makes them one net
# although they sit just as close. The chain runs through V0 / M1 (and V1 /
# M2 for the "remote" variants, whose two ends are far from each other); a
# metal that overlaps both shapes without a via is the "unrelated overlap"
# control and must not connect them.


def _move(shapes: Iterable[Shape], dx: float = 0, dy: float = 0) -> list[Shape]:
    moved: list[Shape] = []
    for sh in shapes:
        if len(sh) == 5:
            layer, x1, y1, x2, y2 = sh  # type: ignore[misc]
            moved.append((layer, x1 + dx, y1 + dy, x2 + dx, y2 + dy))
        else:
            layer, hull, holes = sh  # type: ignore[misc]
            moved.append(
                (
                    layer,
                    tuple((x + dx, y + dy) for x, y in hull),
                    tuple(tuple((x + dx, y + dy) for x, y in h) for h in holes),
                )
            )
    return moved


def _lig_lisd_pair(gap: float) -> list[Shape]:
    """A vertical 18 x 100 nm LIG bar and a 24 x 100 nm LISD bar ``gap`` nm
    to its right."""
    return [box("LIG", 0, 0, 18, 100), box("LISD", 18 + gap, 0, 42 + gap, 100)]


def _bridge_m1(
    gap: float, *, lig_via: bool = True, lisd_via: bool = True
) -> list[Shape]:
    """An M1 bar along the bottom of both bars of :func:`_lig_lisd_pair`,
    with a V0 on each (the V0 is left out where ``*_via`` is false)."""
    shapes: list[Shape] = [box("M1", 0, 0, 39 + gap, 18)]
    if lig_via:
        shapes.append(box("V0", 0, 0, 18, 18))
    if lisd_via:
        shapes.append(box("V0", 21 + gap, 0, 39 + gap, 18))
    return shapes


def _remote_m2(gap: float, *, far_via: bool = True) -> list[Shape]:
    """Both bars of :func:`_lig_lisd_pair` reach an M2 bar only through a
    V0 / M1 / V1 stub each, on opposite sides and ends, so nothing local
    joins them. The M2 bar also runs over the top of both bars with no via
    there. ``far_via`` False omits the V1 under the LISD side's M2 end."""
    shapes: list[Shape] = [
        box("V0", 0, 0, 18, 18),
        box("M1", -60, 0, 18, 18),
        box("V1", -60, 0, -42, 18),
        box("V0", 21 + gap, 82, 39 + gap, 100),
        box("M1", 21 + gap, 82, 120 + gap, 100),
        box("M2", -60, 0, -42, 100),
        box("M2", -60, 82, 120 + gap, 100),
    ]
    if far_via:
        shapes.append(box("V1", 102 + gap, 82, 120 + gap, 100))
    return shapes


def _corner_pair(dx: float, dy: float) -> list[Shape]:
    """LIG bar and a LISD bar above and to the right of its top-right corner
    by ``dx`` / ``dy`` nm (no facing edges)."""
    return [
        box("LIG", 0, 0, 18, 100),
        box("LISD", 18 + dx, 100 + dy, 42 + dx, 200 + dy),
    ]


def _corner_bridge(dx: float, dy: float, *, lisd_via: bool = True) -> list[Shape]:
    """An L-shaped M1 from the LIG bar's top to the corner LISD bar's bottom
    (V0 on each; the LISD one is left out if ``lisd_via`` is false), joining
    the two shapes of :func:`_corner_pair`."""
    shapes = [
        box("V0", 0, 82, 18, 100),
        box("M1", 0, 82, 39 + dx, 100),
        box("M1", 21 + dx, 82, 39 + dx, 118 + dy),
    ]
    if lisd_via:
        shapes.append(box("V0", 21 + dx, 100 + dy, 39 + dx, 118 + dy))
    return shapes


def _sdt_pair(gap: float) -> list[Shape]:
    """A LIG bar ``gap`` nm right of an SDT on ACTIVE, with the SDT sticking
    4 nm out of its LISD (SDT.LISD.AUX.4): LIG and LISD are then ``gap + 4``
    nm apart, so only LIG.SDT.S.8 can see a pair at 14 nm."""
    return [
        *active_in("NSELECT", 0, 0, 70, 81),
        box("SDT", 10, 0, 34, 81),
        box("LISD", -14, 0, 30, 81),
        box("LIG", 34 + gap, 0, 52 + gap, 100),
    ]


def _sdt_bridge(gap: float, *, lig_via: bool = True) -> list[Shape]:
    """A V0 / M1 / V0 chain from the LISD of :func:`_sdt_pair` to its LIG,
    over their tops (ACTIVE and select end at y = 81); ``lig_via`` False
    leaves the M1 lying over the LIG with no V0 on it."""
    shapes = [
        box("V0", -11, 82, 7, 100),
        box("LISD", -14, 81, 30, 100),
        box("M1", -11, 82, 52 + gap, 100),
    ]
    if lig_via:
        shapes.append(box("V0", 34 + gap, 82, 52 + gap, 100))
    return shapes


#: A two-transistor row: ``device()`` and a copy 162 nm to the right. The
#: first transistor's ACTIVE ends at x = 116 + ``ext``, the second one's
#: starts at x = 208, so ``ext`` = 0 leaves the 92 nm ACTIVE.S.2A distance.
#: ``device()`` has SDT/LISD columns at x 42..66 and 96..120 (and 204..228,
#: 258..282 in the copy); the one at 96..120 and the one at 204..228 are the
#: pair that faces the gap.
_ROW_DX = 162


def _row(ext: float = 0) -> list[Shape]:
    """The row without the two dummy GATE that flank the gap: moving ACTIVE's
    end by ``ext`` would otherwise break GATE.ACTIVE.S.4 as well."""
    first = [sh for sh in device(ax1=116 + ext) if not _is_gate(sh, 125)]
    second = [sh for sh in device() if not _is_gate(sh, 17)]
    return [*first, *_move(second, _ROW_DX)]


def _is_gate(shape: Shape, x1: float) -> bool:
    return len(shape) == 5 and shape[0] == "GATE" and shape[1] == x1


def _row_links(
    *, left: bool = False, right: bool = True, m2: bool = False
) -> list[Shape]:
    """Conductors over :func:`_row`. ``right`` joins the two LISD columns that
    face the gap (V0 / M1 / V0); ``left`` instead runs the M1 from the first
    transistor's far LISD column to the second's, passing over its facing one
    with no via (a bridge to the wrong source/drain region); ``m2`` joins
    the facing columns through M2 instead of M1."""
    low = 48  # V0 / M1 / V1 centre height inside the 27..108 nm ACTIVE
    if m2:
        return [
            box("V0", 99, low, 117, low + 18),
            box("M1", 99, low, 117, low + 28),
            box("V1", 99, low, 117, low + 18),
            box("V0", 207, low, 225, low + 18),
            box("M1", 207, low, 225, low + 28),
            box("V1", 207, low, 225, low + 18),
            box("M2", 99, low, 225, low + 18),
        ]
    if left:
        return [
            box("V0", 45, low, 63, low + 18),
            box("V0", 207, low, 225, low + 18),
            box("M1", 45, low, 225, low + 18),
        ]
    shapes = [box("M1", 99, low, 225, low + 18)]
    if right:
        shapes += [
            box("V0", 99, low, 117, low + 18),
            box("V0", 207, low, 225, low + 18),
        ]
    return shapes


def _notch(gap: float, *, bridge: bool = False) -> list[Shape]:
    """Fig. 3.5.2(b): one ACTIVE polygon, a 27 nm bar with a 54 nm lobe at
    each end, ``gap`` nm apart across a notch. Two GATE cross the bar inside
    the notch, so the lobes are source/drain regions of different
    transistors (separated by two channels) although they are one polygon.
    Each lobe has an SDT / LISD column; ``bridge`` joins them (V0 / M1)."""
    x2 = 116 + gap
    outline = poly(
        "ACTIVE",
        (46, 27), (x2 + 70, 27), (x2 + 70, 108), (x2, 108),
        (x2, 54), (116, 54), (116, 108), (46, 108),
    )  # fmt: skip
    shapes: list[Shape] = [
        outline,
        box("NSELECT", 0, 0, x2 + 116, 135),
        box("GATE", 124, 18, 144, 58),
        box("GATE", 178, 18, 198, 58),
        box("SDT", 60, 27, 84, 108),
        box("LISD", 60, 27, 84, 108),
        box("SDT", x2 + 28, 27, x2 + 52, 108),
        box("LISD", x2 + 28, 27, x2 + 52, 108),
    ]
    if bridge:
        shapes += [
            box("V0", 63, 72, 81, 90),
            box("V0", x2 + 31, 72, x2 + 49, 90),
            box("M1", 63, 72, x2 + 49, 90),
        ]
    return shapes


def _net_cases() -> list[Case]:
    c: list[Case] = []
    # ---- LIG.LISD.S.6 / LIG.SDT.S.8: side to side ----
    # LIG and LISD at exactly 14 nm pass, one dbu closer fails (no SDT here,
    # so only S.6 applies).
    for gap, mk in ((14 - D, _fail), (14, _pass)):
        c.append(mk("LIG.LISD.S.6", _lig_lisd_pair(gap)))
    # A 10 nm pair is a violation unless one net joins it: through an M1
    # (direct) or through M2 (the two ends far apart). Without the second V0 /
    # V1, or with a metal that merely overlaps both, it stays a violation.
    c += [
        _fail("LIG.LISD.S.6", [*_lig_lisd_pair(10), *_bridge_m1(10, lisd_via=False)]),
        _fail("LIG.LISD.S.6", [*_lig_lisd_pair(10), *_bridge_m1(10, lig_via=False)]),
        _fail("LIG.LISD.S.6", [*_lig_lisd_pair(10), *_remote_m2(10, far_via=False)]),
        _pass("LIG.LISD.S.6", [*_lig_lisd_pair(10), *_bridge_m1(10)]),
        _pass("LIG.LISD.S.6", [*_lig_lisd_pair(10), *_remote_m2(10)]),
    ]
    # Two LIG on one GATE: spacing to a LISD within 14 nm is judged on the
    # LISD's own net, so a pair joined only through that GATE passes. (Left
    # for the S.2A row below, which also checks a GATE does not join the
    # source/drain regions either side of it.)
    # Overlap is one conductor, whatever the spacing check sees.
    c.append(
        _pass(
            "LIG.LISD.S.6",
            [box("LISD", 0, 0, 24, 100), box("LIG", 16, 40, 116, 58)],
        )
    )
    # ---- LIG.LISD.S.7: corner to corner ----
    # dx = 9, dy = 12 nm is 15 nm apart (a 3-4-5 triangle): pass; a dbu
    # closer is a corner violation but still 14.8 nm, so not LIG.LISD.S.6.
    for dy, mk in ((12 - D, _fail), (12, _pass)):
        c.append(mk("LIG.LISD.S.7", _corner_pair(9, dy)))
    c += [
        _pass("LIG.LISD.S.7", [*_corner_pair(9, 12 - D), *_corner_bridge(9, 12 - D)]),
        _fail(
            "LIG.LISD.S.7",
            [*_corner_pair(9, 12 - D), *_corner_bridge(9, 12 - D, lisd_via=False)],
        ),
    ]
    # Below 14 nm a corner pair breaks both rules.
    c.append(_fail("LIG.LISD.S.6", _corner_pair(9, 9), also=["LIG.LISD.S.7"]))
    # Facing edges that overlap by one dbu are a side pair (14.5 nm: legal
    # under S.6), not a corner pair, so S.7 does not see them.
    c.append(
        _pass(
            "LIG.LISD.S.7",
            [box("LIG", 0, 0, 18, 100), box("LISD", 32.5, 100 - D, 56.5, 200 - D)],
        )
    )
    # ---- LIG.SDT.S.8 ----
    c += [
        _fail("LIG.SDT.S.8", _sdt_pair(14 - D), also=["SDT.LISD.AUX.4"]),
        _pass("LIG.SDT.S.8", _sdt_pair(14), also=["SDT.LISD.AUX.4"]),
        # Nothing joins them, or something unrelated overlaps both.
        _fail("LIG.SDT.S.8", _sdt_pair(10), also=["SDT.LISD.AUX.4"]),
        _fail(
            "LIG.SDT.S.8",
            [*_sdt_pair(10), *_sdt_bridge(10, lig_via=False)],
            also=["SDT.LISD.AUX.4"],
        ),
        _pass(
            "LIG.SDT.S.8",
            [*_sdt_pair(10), *_sdt_bridge(10)],
            also=["SDT.LISD.AUX.4"],
        ),
    ]
    # SDT inside its LISD: the two rules see the same 14 nm.
    for gap, mk in ((14 - D, _fail), (14, _pass)):
        c.append(
            mk(
                "LIG.SDT.S.8",
                [
                    *active_in("NSELECT", 0, 0, 70, 81),
                    box("SDT", 10, 0, 34, 81),
                    box("LISD", 10, 0, 34, 81),
                    box("LIG", 34 + gap, 0, 52 + gap, 100),
                ],
                also=[] if mk is _pass else ["LIG.LISD.S.6"],
            )
        )
    # ---- ACTIVE.S.2A ----
    # ext = 0 leaves exactly 92 nm; a dbu of extension takes it to 91.75.
    c += [
        _fail("ACTIVE.S.2A", _row(D)),
        _pass("ACTIVE.S.2A", _row(0)),
        # 91.75 nm apart is fine when the two regions are one net.
        _pass("ACTIVE.S.2A", [*_row(D), *_row_links()]),
        _pass("ACTIVE.S.2A", [*_row(D), *_row_links(m2=True)]),
        # A metal over both regions with no via joins nothing.
        _fail("ACTIVE.S.2A", [*_row(D), *_row_links(right=False)]),
        # The bridge reaches the far column of the first transistor, not the
        # region facing the gap: a GATE and ACTIVE do not join its two sides.
        _fail("ACTIVE.S.2A", [*_row(D), *_row_links(left=True)]),
    ]
    c += [
        _fail("ACTIVE.S.2A", _notch(92 - D)),
        _pass("ACTIVE.S.2A", _notch(92)),
        _pass("ACTIVE.S.2A", _notch(92 - D, bridge=True)),
        # Note 2 of Table 3.5.2: only horizontal spacing is checked. Two
        # transistors on different nets 54 nm apart vertically pass.
        _pass("ACTIVE.S.2A", [*device(), *_move(device(), dy=135)]),
    ]
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


#: V8 / V9 -> (metal below, layer above: M9 for V8, PAD for V9).
_TOP_VIAS: dict[str, tuple[str, str | None]] = {"V8": ("M8", "M9"), "V9": ("M9", "PAD")}


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
        *_thickox_cases(),
        *_sram_cases(),
        *_sdt_cases(),
        *_lisd_cases(),
        *_lig_cases(),
        *_sram_sdt_cases(),
        *_sram_lisd_cases(),
        *_sram_lig_cases(),
        *_net_cases(),
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


def topology_gap_probes() -> list[Case]:
    """Drawings for the two topology gaps (``GATE.AUX.2``, ``ACTIVE.AUX.3``,
    #3086), each ``expect`` being the rules the deck trips *today*.

    They are not part of :func:`all_cases`: neither rule is implemented, so
    the cases pin that the gap is real (the DRM's illegal drawing passes the
    deck) and which existing rules already cover part of it. Gates sit between
    two neighbour columns as tall as the stack so ``GATE.S.3`` stays quiet;
    ACTIVE is 162 nm tall (6 x 27 nm) with the slot 54 nm up, so every strip
    left beside a slot meets ``ACTIVE.W.1`` and the full-height and bottom
    strips meet ``ACTIVE.W.2``; the strip above a slot is a multiple of 27 nm
    only for a 27 nm slot, so the other slot heights also trip ``ACTIVE.W.2``
    (an existing rule reporting the strip, not the notch)."""

    def column(x: float, y0: float, y1: float) -> Shape:
        return box("GATE", x, y0, x + 20, y1)

    def gates(*columns: Shape) -> list[Shape]:
        top = max(s[4] for s in columns if len(s) == 5 and s[0] == "GATE")
        return [*columns, column(-54, 0, top), column(-108, 0, top)]

    def slot(height: float, depth: float) -> list[Shape]:
        """A rectangular slot cut into the right (vertical) edge."""
        y0 = 54
        hull = (
            (0, 0), (100, 0), (100, y0), (100 - depth, y0),
            (100 - depth, y0 + height), (100, y0 + height), (100, 162), (0, 162),
        )  # fmt: skip
        return [poly("ACTIVE", *hull), box("NSELECT", -46, -27, 146, 189)]

    def active(*hull: Point) -> list[Shape]:
        return [poly("ACTIVE", *hull), box("NSELECT", -46, -27, 146, 189)]

    def slot_expect(height: float) -> frozenset[str]:
        rules = {"ACTIVE.S.1"} if height < 27 else set()
        if height != 27:
            rules.add("ACTIVE.W.2")
        return frozenset(rules)

    c = [
        # GATE.AUX.2: Fig. 3.4.2(a) drawn at several gaps, and (d) as one
        # polygon under GCUT.
        *(
            Case(f"gate-stack-gap{g}", "probe", tuple(gates(column(0, 0, 40),
               column(0, 40 + g, 80 + g))))
            for g in (1, 20, 50, 100, 500, 2000)
        ),
        Case("gate-stack-gcut-over-gap", "probe", tuple(gates(
            column(0, 0, 40), column(0, 80, 120), box("GCUT", -17, 40, 37, 80)))),
        Case("gate-single-gcut", "probe", tuple(gates(
            column(0, 0, 120), box("GCUT", -17, 40, 37, 80)))),
        Case("gate-stack-abut", "probe", tuple(gates(
            column(0, 0, 40), column(0, 40, 80)))),
        # ACTIVE.AUX.3: Fig. 3.5.4(b) slots in a vertical edge; ACTIVE.S.1
        # (27 nm) already reports the narrow ones, ACTIVE.W.2 the strip above.
        *(
            Case(f"active-slot-h{h}", "probe", tuple(slot(h, 20)),
                 slot_expect(h))
            for h in (8, 26, 27, 30, 40)
        ),
        Case("active-slot-h30-d5", "probe", tuple(slot(30, 5)), slot_expect(30)),
        Case("active-slot-h30-d60", "probe", tuple(slot(30, 60)), slot_expect(30)),
        Case("active-L", "probe", tuple(active(
            (0, 0), (100, 0), (100, 54), (50, 54), (50, 162), (0, 162)))),
        Case("active-U-vertical-fingers", "probe", tuple(active(
            (0, 0), (100, 0), (100, 162), (70, 162), (70, 54), (30, 54),
            (30, 162), (0, 162)))),
    ]  # fmt: skip
    return c


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


# --------------------------------------------------------------------------
# ACTIVE.LUP.1 evidence experiments (#3087)
# --------------------------------------------------------------------------
#
# ACTIVE.LUP.1 (DRM Table 3.5.2) is *not* a deck rule, so none of this is in
# all_cases(). Each experiment is its own layout (one top cell, nothing else
# in it), because the 5.376 um case slots are far closer than 30 um and
# neighbouring cases would supply a tap. The reference predicate below
# classifies the *written* layout, so it is not a circular check of the
# generator's own intent.

#: Candidate readings of "distance" between two ACTIVE shapes (the DRM does
#: not pick one); all are edge-to-edge between bounding boxes.
LATCHUP_METRICS = ("euclidean", "manhattan", "chebyshev")
#: The DRM's maximum MOS-to-tap distance, in nm (Table 3.5.2: <= 30 um).
LATCHUP_MAX_NM = 30000.0
#: x of the first feature right of device(): its ACTIVE's right edge.
_MOS_RIGHT = 116.0
_MOS_TOP = 108.0


@dataclass(frozen=True)
class LatchupExperiment:
    name: str
    shapes: tuple[Shape, ...]
    #: Hand-derived (from the DRM reading), per metric: does the leftmost
    #: MOS ACTIVE have an eligible tap within 30 um?
    expect: dict[str, bool]


def _lup(name: str, shapes: Iterable[Shape], **expect: bool) -> LatchupExperiment:
    """``expect`` per metric; a bare ``all=`` sets every metric."""
    if "all" in expect:
        verdict = expect.pop("all")
        expect = dict.fromkeys(LATCHUP_METRICS, verdict)
    assert set(expect) == set(LATCHUP_METRICS), name
    return LatchupExperiment(name, tuple(shapes), dict(expect))


def _tap(sel: str, dx: float, dy: float = 0.0, w: float = 70) -> list[Shape]:
    """A gateless ACTIVE (a bulk contact) ``dx`` / ``dy`` nm past the device's
    ACTIVE edge, inside its own select."""
    x1 = _MOS_RIGHT + dx
    y1 = _MOS_TOP + dy if dy else 27
    return active_in(sel, x1, y1, x1 + w, y1 + 81)


def latchup_experiments() -> list[LatchupExperiment]:
    """Isolated MOS-to-tap experiments for ACTIVE.LUP.1.

    The NMOS sits in the substrate (outside every WELL) and needs a PSELECT
    tap there; the PMOS sits in a WELL and needs an NSELECT tap in the same
    WELL.
    """
    nmos = device(sel="NSELECT")
    pmos = device(sel="PSELECT")
    in_well = box("WELL", -150, -150, 40000, 300)
    exp = [
        _lup("nmos-ptap-5um", [*nmos, *_tap("PSELECT", 5000)], all=True),
        _lup("nmos-ptap-at-30um", [*nmos, *_tap("PSELECT", 30000)], all=True),
        _lup(
            "nmos-ptap-1dbu-past-30um",
            [*nmos, *_tap("PSELECT", 30000 + D)],
            all=False,
        ),
        _lup("nmos-no-tap", nmos, all=False),
        # Ordinary active-to-active: a second MOS 5 um away is not a tap.
        _lup(
            "nmos-ordinary-active-neighbour",
            [*nmos, *(_shift(s, 5000 + _MOS_RIGHT - 46) for s in pmos)],
            all=False,
        ),
        # Wrong class: an N+ gateless ACTIVE in the substrate is not a
        # substrate (P+) contact.
        _lup("nmos-wrong-class-ntap", [*nmos, *_tap("NSELECT", 5000)], all=False),
        # Right class, wrong well: a P+ ACTIVE inside a WELL contacts the
        # WELL, not the substrate the NMOS is in.
        _lup(
            "nmos-ptap-in-well",
            [
                *nmos,
                *_tap("PSELECT", 5000),
                box("WELL", _MOS_RIGHT + 5000 - 100, -50, _MOS_RIGHT + 5200, 200),
            ],
            all=False,
        ),
        _lup(
            "pmos-ntap-same-well-5um",
            [*pmos, in_well, *_tap("NSELECT", 5000)],
            all=True,
        ),
        _lup(
            "pmos-ntap-same-well-at-30um",
            [*pmos, in_well, *_tap("NSELECT", 30000)],
            all=True,
        ),
        # Same class, but in a different WELL polygon (>= 108 nm apart).
        _lup(
            "pmos-ntap-other-well",
            [
                *pmos,
                box("WELL", -150, -150, _MOS_RIGHT + 3000, 300),
                *_tap("NSELECT", 5000),
                box("WELL", _MOS_RIGHT + 4800, -150, _MOS_RIGHT + 6000, 300),
            ],
            all=False,
        ),
        # Diagonal pairs: the verdict depends on the undefined metric.
        _lup(
            "nmos-ptap-diagonal-21um",
            [*nmos, *_tap("PSELECT", 21000, 21000)],
            euclidean=True,  # hypot(21, 21) = 29.70 um
            manhattan=False,  # 42 um
            chebyshev=True,  # 21 um
        ),
        _lup(
            "nmos-ptap-diagonal-22um",
            [*nmos, *_tap("PSELECT", 22000, 22000)],
            euclidean=False,  # hypot(22, 22) = 31.11 um
            manhattan=False,  # 44 um
            chebyshev=True,  # 22 um
        ),
    ]
    return exp


def _shift(shape: Shape, dx: float) -> Shape:
    assert len(shape) == 5, "latch-up experiments only shift boxes"
    layer, x1, y1, x2, y2 = shape  # type: ignore[misc]
    return (layer, x1 + dx, y1, x2 + dx, y2)


def _gap_nm(a: kdb.Box, b: kdb.Box, metric: str) -> float:
    dx = max(0, a.left - b.right, b.left - a.right) * D
    dy = max(0, a.bottom - b.top, b.bottom - a.top) * D
    if metric == "euclidean":
        return math.hypot(dx, dy)
    if metric == "manhattan":
        return dx + dy
    assert metric == "chebyshev", metric
    return max(dx, dy)


def latchup_reference(path: str, metric: str) -> bool:
    """Reference reading of ACTIVE.LUP.1 on a written experiment layout:
    does its leftmost MOS ACTIVE (ACTIVE crossed by GATE; a second MOS is an
    ordinary-active control, never a tap) have an eligible tap within 30 um?

    A tap is a gateless ACTIVE whose select is the opposite polarity of the
    MOS's (a substrate / WELL contact has the well's own dopant type) and
    that lies in the same WELL polygon, or, for a MOS outside every WELL,
    also outside every WELL. Connectivity is not modelled (the DRM states
    none). ACTIVE shapes must be boxes.
    """
    layout = kdb.Layout()
    layout.read(path)
    top = layout.top_cell()

    def region(name: str) -> kdb.Region:
        return kdb.Region(top.begin_shapes_rec(layout.layer(LAYERS[name], 0))).merged()

    active, gate = region("ACTIVE"), region("GATE")
    nsel, wells = region("NSELECT"), list(region("WELL").each())
    mos = active.interacting(gate)
    taps = active.not_interacting(gate)
    assert mos.count() >= 1, "every experiment draws a MOS"

    def sel_of(poly: kdb.Polygon) -> str:
        return "NSELECT" if not (kdb.Region(poly) & nsel).is_empty() else "PSELECT"

    def well_of(poly: kdb.Polygon) -> int:
        for i, w in enumerate(wells):
            if not kdb.Region(poly).inside(kdb.Region(w)).is_empty():
                return i
        return -1

    m = min(mos.each(), key=lambda p: p.bbox().left)  # the device under test
    assert m.is_box()
    for t in taps.each():
        assert t.is_box()
        if sel_of(t) == sel_of(m):
            continue
        if well_of(t) != well_of(m):
            continue
        if _gap_nm(m.bbox(), t.bbox(), metric) <= LATCHUP_MAX_NM:
            return True
    return False


def write_latchup_experiment(path: str, exp: LatchupExperiment) -> None:
    """Write one experiment as its own single-top-cell GDS."""
    write_shapes(path, exp.shapes)
