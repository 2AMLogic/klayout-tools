"""Measured gap reasons for V0 in ASAP7 DRM Tables 3.11.1 and 3.11.2."""

CORNER_SPACING = (
    "At T-shaped M1 landings a 25 nm corner gap can face two flush ends on one "
    "axis and two 5 nm caps on the other. The 30 and 23 nm bounds give different "
    "results."
)

LISD_EXACT_3 = (
    "'== 3 nm on at least two opposite sides' is expressible without a routing "
    "direction, but the literal '==' contradicts the pinned shipped cells (100 "
    "V0 in 57 of 212 RVT cells are enclosed by LISD by more than 3 nm on every "
    "opposite pair, e.g. 4 / 2 nm across and 27 / 36 nm along); the cells "
    "satisfy only the 'at least 3 nm' reading, so the exact check is not shipped"
)

LIG_ENCLOSURE = (
    "V0 encloses LIG by at least 1 nm on opposite sides. Each pinned RVT, LVT "
    "and SLVT library has 1,413 selected V0 wholly inside LIG."
)

LIG_DIRECTION = (
    "In each pinned library, 4,899 selected cuts touch horizontal LIG only on their "
    "vertical V0 edges. A square LIG has no unique length axis."
)
