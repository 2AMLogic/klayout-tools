"""Synthetic edge-column variants for the STA stage-row parser (issue #6479).

These rows test the parser's accepted grammar; they do not establish that
an engine emits data rows without an edge column. Captured-engine tests
remain in ``test_post_route_sta_stages.py``.
"""

import pytest

from klayout_tools.post_route_sta_stages import _stage_rows


@pytest.mark.parametrize("pin_index", [0, 1, 2], ids=["start", "middle", "end"])
@pytest.mark.parametrize("edge", ["", "v", "^", "vv", "^v"])
@pytest.mark.parametrize("separator", [" ", "   ", "\t"])
def test_stage_rows_preserve_v_prefixed_pin(pin_index, edge, separator):
    """Exercise each window position independently, including absent edges."""
    pins = ["ff_in/Q", "b1/X", "ff_out/D"]
    pins[pin_index] = "vbuf/X"
    region = [
        f"0.250000 0.250000 v {pins[0]} (flop)",
        f"0.125000 0.375000 v {pins[1]} (buffer)",
        f"0.000000 0.375000 v {pins[2]} (flop)",
    ]
    region[pin_index] = region[pin_index].replace(
        " v ", separator + (edge + separator if edge else "")
    )

    rows, reason = _stage_rows(region, pins[0], pins[-1])

    assert reason is None
    assert rows == [
        {"pin": pins[0], "cell": "flop", "incr_ns": 0.25, "path_ns": 0.25},
        {"pin": pins[1], "cell": "buffer", "incr_ns": 0.125, "path_ns": 0.375},
        {"pin": pins[2], "cell": "flop", "incr_ns": 0.0, "path_ns": 0.375},
    ]
