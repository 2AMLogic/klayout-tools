"""``.meas`` card generation for ``klt characterize``: the four NLDM timing
quantities and the per-edge supply-charge integrals of one grid point.

Split out of :mod:`klayout_tools.characterize` (size ratchet, issue #4755).
"""

from __future__ import annotations

import re
from typing import Any

from . import liberty_writer
from .characterize_arcs import DerivedArc


def _spice_number(value: float) -> str:
    return liberty_writer.format_number(value, precision=10)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def measurement_cards(
    *,
    tag: str,
    arc: DerivedArc,
    vdd: float,
    thresholds: liberty_writer.Thresholds,
    clock_edge: str = "rising",
    edge_starts: tuple[float, float] = (0.0, 0.0),
) -> list[dict[str, Any]]:
    output_node = f"o_{tag}_{_slug(arc.output_pin)}"
    input_node = f"i_{tag}"

    # Which *input* edge produces the rising output edge, under this arc's
    # own measured polarity.
    rise_input_edge = "RISE" if arc.measured_sense == "positive_unate" else "FALL"
    fall_input_edge = "FALL" if arc.measured_sense == "positive_unate" else "RISE"
    if arc.is_sequential:
        # Both output edges are caused by the clock's active edge.
        rise_input_edge = fall_input_edge = "RISE" if clock_edge == "rising" else "FALL"

    v_in_rise = vdd * thresholds.input_threshold_pct_rise / 100.0
    v_in_fall = vdd * thresholds.input_threshold_pct_fall / 100.0
    v_out_rise = vdd * thresholds.output_threshold_pct_rise / 100.0
    v_out_fall = vdd * thresholds.output_threshold_pct_fall / 100.0
    v_slew_lo_rise = vdd * thresholds.slew_lower_threshold_pct_rise / 100.0
    v_slew_hi_rise = vdd * thresholds.slew_upper_threshold_pct_rise / 100.0
    v_slew_lo_fall = vdd * thresholds.slew_lower_threshold_pct_fall / 100.0
    v_slew_hi_fall = vdd * thresholds.slew_upper_threshold_pct_fall / 100.0

    trig_rise_v = v_in_rise if rise_input_edge == "RISE" else v_in_fall
    trig_fall_v = v_in_rise if fall_input_edge == "RISE" else v_in_fall

    cards = [
        (
            f"{tag}_dr",
            f"TRIG v({input_node}) VAL={_spice_number(trig_rise_v)} "
            f"{rise_input_edge}=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_out_rise)} RISE=1",
        ),
        (
            f"{tag}_df",
            f"TRIG v({input_node}) VAL={_spice_number(trig_fall_v)} "
            f"{fall_input_edge}=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_out_fall)} FALL=1",
        ),
        (
            f"{tag}_tr",
            f"TRIG v({output_node}) VAL={_spice_number(v_slew_lo_rise)} RISE=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_slew_hi_rise)} RISE=1",
        ),
        (
            f"{tag}_tf",
            f"TRIG v({output_node}) VAL={_spice_number(v_slew_hi_fall)} FALL=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_slew_lo_fall)} FALL=1",
        ),
    ]
    if arc.is_sequential:
        # Skip the priming pulse (and the other measured pulse) with TD: the
        # output-rise edge belongs to the first measured pulse, the fall to
        # the second. Cards are dr, df, tr, tf in that order.
        rise_td, fall_td = (_spice_number(t) + "n" for t in edge_starts)
        tds = (rise_td, fall_td, rise_td, fall_td)
        cards = [
            (name, body.replace(" TARG ", f" TD={td} TARG ") + f" TD={td}")
            for (name, body), td in zip(cards, tds, strict=True)
        ]
    return [
        {"name": name, "spice": f".meas tran {name} {body}", "unit": "s"}
        for name, body in cards
    ]


#: The four supply-charge integrals per grid point, and what each measures:
#: ``(suffix, ammeter prefix, output edge)``. ``qv*`` is charge drawn from
#: the vdd rail, ``qg*`` charge returned to the gnd rail, over the window
#: of the input edge that produces the named *output* edge.
_POWER_CARDS = (
    ("qvr", "Vpd", "rise"),
    ("qgr", "Vpg", "rise"),
    ("qvf", "Vpd", "fall"),
    ("qgf", "Vpg", "fall"),
)


def power_cards(
    *,
    tag: str,
    arc: DerivedArc,
    windows: dict[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    """``INTEG`` cards for the four per-edge rail charges of one grid point.

    The output-rise window is the *input* edge's window that produces it --
    the rising input's for a positive-unate measured polarity, the falling
    input's otherwise -- mirroring :func:`_measurement_cards`'s trigger
    choice so a table can never pair one edge's delay with the other edge's
    energy.
    """
    rise_input_edge = "RISE" if arc.measured_sense == "positive_unate" else "FALL"
    fall_input_edge = "FALL" if arc.measured_sense == "positive_unate" else "RISE"
    window_of = {
        "rise": windows[rise_input_edge],
        "fall": windows[fall_input_edge],
    }
    cards = []
    for suffix, ammeter, edge in _POWER_CARDS:
        start, stop = window_of[edge]
        cards.append(
            {
                "name": f"{tag}_{suffix}",
                "spice": (
                    f".meas tran {tag}_{suffix} INTEG i({ammeter}{tag}) "
                    f"FROM={_spice_number(start)}n TO={_spice_number(stop)}n"
                ),
                "unit": "C",
            }
        )
    return cards
