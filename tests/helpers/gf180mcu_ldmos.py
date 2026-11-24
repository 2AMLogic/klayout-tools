"""Synthesized gf180mcu drain-extended LDMOS layouts (issue #4570), shared by
the extraction tests and the native-LVS cross-check. Geometry follows the
PDK's own ``mos_derivations.lvs`` (``ngate_ldmos``/``nd_ldmos``/``ns_ldmos`` and
the ``pgate_ldmos`` mirror): the drain marker (``MVSD`` 210/0 for the NMOS,
``MVPSD`` 11/39 for the PMOS) overlaps the drain end of the poly gate."""

from __future__ import annotations

import klayout.db as kdb

_DBU_UM = 0.001


def _box(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(*(round(v / _DBU_UM) for v in (x0, y0, x1, y1)))


def make_ldmos_layout(
    kind: str,
    *,
    mirrored: bool = False,
    drain_marker: bool = True,
    ldmos_xtor: bool = True,
) -> kdb.Layout:
    """One LDMOS (``kind`` ``"n"`` or ``"p"``), gate L=0.5um, W=1um, source
    on the left and drain (marker-covered) on the right, or mirrored.

    Pins are labelled ``S``/``D``/``G`` (and ``B`` for the PMOS well tie).
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer: int, datatype: int, text: str, x: float, y: float) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    def mx(x0: float, x1: float) -> tuple[float, float]:
        return (3.0 - x1, 3.0 - x0) if mirrored else (x0, x1)

    if kind == "n":
        draw(32, 0, _box(-0.5, -0.5, 3.5, 1.5))  # Nplus
    else:
        draw(31, 0, _box(-0.6, -0.6, 3.7, 1.7))  # Pplus
        draw(12, 0, _box(-1.5, -1.2, 4.2, 2.2))  # DNWELL
        # Nwell tap ring segment tying the DNWELL body to "B".
        draw(21, 0, _box(-1.4, -0.1, -0.5, 1.1))
        draw(32, 0, _box(-1.35, 0.05, -0.55, 0.95))
        draw(22, 0, _box(-1.3, 0.1, -0.6, 0.9))
        draw(33, 0, _box(-1.1, 0.4, -0.9, 0.6))
        draw(34, 0, _box(-1.2, 0.3, -0.8, 0.7))
        label(34, 10, "B", -1.0, 0.5)
    draw(22, 0, _box(0, 0, 3, 1))  # Comp
    draw(30, 0, _box(*mx(0.8, 1.6)[:1], -0.2, mx(0.8, 1.6)[1], 1.2))  # Poly2
    draw(55, 0, _box(-0.8, -0.8, 3.8, 1.8))  # Dualgate
    if ldmos_xtor:
        draw(226, 0, _box(-0.6, -0.6, 3.6, 1.6))  # LDMOS_XTOR
    if drain_marker:
        x0, x1 = mx(1.3, 3.4)
        draw(
            210 if kind == "n" else 11,
            0 if kind == "n" else 39,
            _box(x0, -0.4, x1, 1.4),
        )

    s0, s1 = mx(0.1, 0.3)
    d0, d1 = mx(2.7, 2.9)
    draw(33, 0, _box(s0, 0.3, s1, 0.7))
    draw(33, 0, _box(d0, 0.3, d1, 0.7))
    ps0, ps1 = mx(0.0, 0.4)
    pd0, pd1 = mx(2.6, 3.0)
    draw(34, 0, _box(ps0, 0.2, ps1, 0.8))
    draw(34, 0, _box(pd0, 0.2, pd1, 0.8))
    label(34, 10, "S", (ps0 + ps1) / 2, 0.5)
    label(34, 10, "D", (pd0 + pd1) / 2, 0.5)
    draw(
        33,
        0,
        _box(0.9 if not mirrored else 1.9, 1.0, 1.1 if not mirrored else 2.1, 1.2),
    )
    gx = 1.0 if not mirrored else 2.0
    draw(34, 0, _box(gx - 0.15, 0.95, gx + 0.15, 1.25))
    label(34, 10, "G", gx, 1.1)
    return layout
