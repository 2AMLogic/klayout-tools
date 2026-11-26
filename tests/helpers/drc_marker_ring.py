"""Independent intersection checks for DRC marker outlines, using exact fractions."""

from __future__ import annotations

from fractions import Fraction


def _sides_meet(p, q, r, s) -> bool:
    """Exact test: do the closed segments ``p-q`` and ``r-s`` share a point?

    Solved parametrically over ``Fraction``s -- deliberately a different
    formulation from the orientation test in the module under test, so the
    two cannot share a bug.
    """
    d1 = (q[0] - p[0], q[1] - p[1])
    d2 = (s[0] - r[0], s[1] - r[1])
    w = (r[0] - p[0], r[1] - p[1])
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if denom != 0:
        t = Fraction(w[0] * d2[1] - w[1] * d2[0], denom)
        u = Fraction(w[0] * d1[1] - w[1] * d1[0], denom)
        return 0 <= t <= 1 and 0 <= u <= 1
    if w[0] * d1[1] - w[1] * d1[0] != 0:
        return False  # parallel, on different lines

    # Collinear: the two sides meet iff their projections onto d1 overlap.
    def proj(pt):
        return (pt[0] - p[0]) * d1[0] + (pt[1] - p[1]) * d1[1]

    a_lo, a_hi = 0, proj(q)
    b_lo, b_hi = sorted((proj(r), proj(s)))
    return max(a_lo, b_lo) <= min(a_hi, b_hi)


def first_meeting_sides(points) -> tuple[int, int] | None:
    """The first pair of non-adjacent sides of the closed ring ``points``
    that meet (the closing side counts as adjacent to the first), or
    ``None`` when the ring is simple."""
    n = len(points)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _sides_meet(
                points[i], points[(i + 1) % n], points[j], points[(j + 1) % n]
            ):
                return i, j
    return None


def assert_simple_quad(points, corners=None) -> None:
    """``points`` is a four-corner perimeter: four distinct vertices (the
    set equal to ``corners`` when given) and no two non-adjacent sides --
    the closing side included -- meeting."""
    assert len(points) == 4, points
    assert len({tuple(p) for p in points}) == 4, points
    if corners is not None:
        assert {tuple(p) for p in points} == {tuple(c) for c in corners}
    assert first_meeting_sides(points) is None, (
        f"non-adjacent sides {first_meeting_sides(points)} of {points} meet"
    )
