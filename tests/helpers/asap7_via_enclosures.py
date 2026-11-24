"""Lower-metal enclosure fixtures for an 18 × 18 nm via at (0, 0)."""


def cap(
    e0: float, e1: float, vertical: bool = True
) -> tuple[float, float, float, float]:
    # 4 nm across the bar falls short of 5 nm on both sides. Its 26 nm
    # width keeps M2/M3 above their 504 nm² minimum area in the boundary cases.
    return (-4, -e0, 22, 18 + e1) if vertical else (-e0, -4, 18 + e1, 22)


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
    ((cap(8, 8),), ()),
    (ONE_TOP_NOTCH, NOTCH_RULES),
    (TWO_TOP_NOTCHES, NOTCH_RULES),
)
V1_FAIL = (
    ((cap(4, 4),), ()),
    ((cap(8, 0),), ()),
    ((cap(5, 0),), ()),
    (BOTH_ENDS_NOTCHED, NOTCH_RULES),
)
