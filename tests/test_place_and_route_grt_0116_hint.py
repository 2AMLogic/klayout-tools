"""GRT-0116 route-failure congestion summary (issue #5122)."""

from __future__ import annotations

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route

_GRT_0116_TABLE = """\
[INFO GRT-0096] Final congestion report:
Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Overflow
---------------------------------------------------------------------------------------
met1               2811           695           24.72%            0 /  0 /  0
met2               2500          2600          104.00%            3 /  5 /  12
Total              5311          3295           62.04%            3 /  5 /  12
[ERROR GRT-0116] Global routing finished with congestion.
"""


def test_engine_error_message_grt_0116_summarizes_congestion():
    completed = fake_completed(returncode=1, stdout=_GRT_0116_TABLE)

    message = place_and_route._engine_error_message("route", completed)

    assert message.startswith("openroad 'route' stage failed: [ERROR GRT-0116]")
    assert "total overflow 12" in message
    assert "worst layer met2 with overflow 12" in message


def test_engine_error_message_grt_0116_without_table_points_at_allow_congestion():
    completed = fake_completed(
        returncode=1,
        stdout="[ERROR GRT-0116] Global routing finished with congestion.\n",
    )

    message = place_and_route._engine_error_message("route", completed)

    assert "route_allow_congestion: true" in message


def test_engine_error_message_without_grt_0116_is_unchanged():
    completed = fake_completed(returncode=1, stdout="[ERROR GRT-0000] something else\n")

    message = place_and_route._engine_error_message("route", completed)

    assert message == "openroad 'route' stage failed: [ERROR GRT-0000] something else"
