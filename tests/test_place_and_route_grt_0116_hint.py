"""GRT-0116 route-failure congestion (issues #5122 and #6952).

The first three tests cover the human-readable hint in the error message
(#5122). The rest are CLI regressions for #6952: a route stage that exits
nonzero still exits 1 with empty stdout, and its JSON error envelope carries
the congestion evidence that attempt produced -- the ``GRT-0096`` table from
the captured stdout and the ranked regions from the
``-congestion_report_file`` report -- with each missing input independently
``null`` and named in ``null_reasons``.

``_GRT_0116_TABLE`` follows the verbatim format of OpenROAD
26Q3-1510-g6cb3f2b704's ``global_route -verbose`` output. The region report
is the repo's real captured fixture (``gcd_global_route_congestion.rpt``,
forced-overflow ``gcd`` run): 217 records, worst overflow 7, ``on Layer -``
throughout. The stubbed route writes that report before exiting nonzero,
exactly where a real GRT-0116 run has already written it by the time it
exits. No live overflowing macro run is claimed: attribution is exercised
against a synthetic macro footprint over the real GCell coordinates.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route
from klayout_tools.cli import main
from klayout_tools.place_and_route_route_overflow import route_failure_congestion
from test_place_and_route import (
    _setup_success_env,
    _stub_openroad_success,
    _write,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_GRT_0116_TABLE = """\
[INFO GRT-0096] Final congestion report:
Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Overflow
---------------------------------------------------------------------------------------
met1               2811           695           24.72%            0 /  0 /  0
met2               2500          2600          104.00%            3 /  5 /  12
Total              5311          3295           62.04%            3 /  5 /  12
[INFO GRT-0014] Routed nets: 492
[ERROR GRT-0116] Global routing finished with congestion.
"""

_GRT_0116_ERROR_ONLY = "[ERROR GRT-0116] Global routing finished with congestion.\n"

_CONGESTION_REPORT_FIXTURE = (
    Path(__file__)
    .joinpath("..", "fixtures", "place_and_route", "gcd_global_route_congestion.rpt")
    .resolve()
)

#: Where the route stage's generated script bakes the
#: `-congestion_report_file` path (`place_and_route_script.py`).
_CONGESTION_REPORT_PATH_RE = re.compile(
    r"global_route -verbose -congestion_report_file (\S+)"
)

#: The fixture's worst overflow region (overflow 7), centred at (10.35,
#: 38.0); the macro the CLI regression places at (0, 30) with SIZE 20x20
#: holds that centre.
_KNOWN_REGION_BBOX = [6.9, 34.5, 13.8, 41.4]


def _write_20x20_macro_lef(path) -> str:
    """A macro LEF whose SIZE 20.0 BY 20.0 footprint, placed at (0, 30),
    holds the fixture's worst overflow GCell -- the attribution probe."""
    return _write(
        path,
        "VERSION 5.7 ;\n"
        "MACRO analog_20x20\n"
        "  CLASS BLOCK ;\n"
        "  SIZE 20.0 BY 20.0 ;\n"
        "  PIN A\n"
        "    DIRECTION INOUT ;\n"
        "    USE ANALOG ;\n"
        "    PORT\n"
        "      LAYER li1 ;\n"
        "        RECT 0.0 0.0 0.2 0.2 ;\n"
        "    END\n"
        "  END A\n"
        "END analog_20x20\n"
        "END LIBRARY\n",
    )


def _stub_openroad_route_failure(
    monkeypatch, *, route_stdout, report_text=None, fail_corner=None
):
    """Floorplan/place/cts succeed (via the shared success stub); the route
    stage exits nonzero with ``route_stdout``, first writing
    ``report_text`` to this run's ``-congestion_report_file`` path when
    given -- the point a real GRT-0116 run has already written its report."""
    _stub_openroad_success(monkeypatch)
    succeeding = place_and_route.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] != ["openroad", "-version"]:
            script_path = cmd[5]
            if Path(script_path).name.endswith("_route.tcl") and (
                fail_corner is None or fail_corner in Path(script_path).parts
            ):
                if report_text is not None:
                    match = _CONGESTION_REPORT_PATH_RE.search(
                        Path(script_path).read_text(encoding="utf-8")
                    )
                    assert match is not None, "no congestion_report_file in script"
                    Path(match.group(1)).write_text(report_text, encoding="utf-8")
                return fake_completed(returncode=1, stdout=route_stdout)
        return succeeding(cmd, **kwargs)

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def _run_cli_json(
    tmp_path,
    monkeypatch,
    capsys,
    *,
    route_stdout,
    report_text=None,
    **request_overrides,
):
    _stub_openroad_route_failure(
        monkeypatch, route_stdout=route_stdout, report_text=report_text
    )
    request_path = _setup_success_env(tmp_path, monkeypatch, **request_overrides)
    exit_code = main(["place-and-route", request_path, "--format", "json"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    return json.loads(captured.err)["error"]


def test_engine_error_message_grt_0116_summarizes_congestion():
    completed = fake_completed(returncode=1, stdout=_GRT_0116_TABLE)

    message = place_and_route._engine_error_message("route", completed)

    assert message.startswith("openroad 'route' stage failed: [ERROR GRT-0116]")
    assert "total overflow 12" in message
    assert "worst layer met2 with overflow 12" in message


def test_engine_error_message_grt_0116_without_table_points_at_allow_congestion():
    completed = fake_completed(returncode=1, stdout=_GRT_0116_ERROR_ONLY)

    message = place_and_route._engine_error_message("route", completed)

    assert "route_allow_congestion: true" in message


def test_engine_error_message_without_grt_0116_is_unchanged():
    completed = fake_completed(returncode=1, stdout="[ERROR GRT-0000] something else\n")

    message = place_and_route._engine_error_message("route", completed)

    assert message == "openroad 'route' stage failed: [ERROR GRT-0000] something else"


def test_cli_grt_0116_exposes_congestion_on_the_error_envelope(
    tmp_path, monkeypatch, capsys
):
    _write_20x20_macro_lef(tmp_path / "analog_20x20.lef")
    error = _run_cli_json(
        tmp_path,
        monkeypatch,
        capsys,
        route_stdout=_GRT_0116_TABLE,
        report_text=_CONGESTION_REPORT_FIXTURE.read_text(encoding="utf-8"),
        macros=[
            {
                "instance": "u_analog",
                "lef": "analog_20x20.lef",
                "x_um": 0.0,
                "y_um": 30.0,
            }
        ],
    )

    assert "[ERROR GRT-0116]" in error["message"]
    assert "total overflow 12" in error["message"]
    assert "worst layer met2 with overflow 12" in error["message"]
    assert "openroad invocation:" in error["message"]

    congestion = error["congestion"]
    gr = congestion["global_route"]
    assert gr["total_overflow"] == 12
    assert gr["max_horizontal_overflow"] == 3
    assert gr["max_vertical_overflow"] == 5
    assert gr["routed_net_count"] == 492
    assert gr["overflow_per_layer"]["met2"]["total_overflow"] == 12

    regions = gr["overflow_regions"]
    assert len(regions) == 20
    groups = [
        g for direction in gr["overflow_by_region"].values() for g in direction.values()
    ]
    assert sum(g["violation_tile_count"] for g in groups) == 217
    assert sum(g["overflow_sum"] for g in groups) == 847
    overflows = [region["overflow"] for region in regions]
    assert overflows == sorted(overflows, reverse=True)
    assert regions[0]["overflow"] == 7
    assert regions[0]["layer"] is None  # `on Layer -` parses to JSON null
    assert all(
        key in regions[0]
        for key in ("direction", "capacity", "usage", "bbox_um", "layer", "macro")
    )

    hit = next(r for r in regions if r["bbox_um"] == _KNOWN_REGION_BBOX)
    assert (hit["macro"], hit["where"]) == ("u_analog", "inside_footprint")

    report_path = gr["congestion_report_path"]
    assert report_path.endswith("gcd_route_congestion.rpt")
    assert os.path.exists(report_path)
    assert list(congestion["null_reasons"]) == ["global_route.max_gcell_usage_pct"]


def test_cli_grt_0116_table_only_names_the_missing_report(
    tmp_path, monkeypatch, capsys
):
    error = _run_cli_json(tmp_path, monkeypatch, capsys, route_stdout=_GRT_0116_TABLE)

    gr = error["congestion"]["global_route"]
    assert gr["total_overflow"] == 12
    assert gr["overflow_regions"] is None
    assert gr["congestion_report_path"] is None
    reasons = error["congestion"]["null_reasons"]
    assert "global_route.overflow_regions" in reasons
    assert "global_route.congestion_report_path" in reasons
    assert "route exited before" in reasons["global_route.congestion_report_path"]
    assert "global_route.total_overflow" not in reasons


def test_cli_grt_0116_report_only_names_the_missing_table(
    tmp_path, monkeypatch, capsys
):
    error = _run_cli_json(
        tmp_path,
        monkeypatch,
        capsys,
        route_stdout=_GRT_0116_ERROR_ONLY,
        report_text=_CONGESTION_REPORT_FIXTURE.read_text(encoding="utf-8"),
    )

    assert "route_allow_congestion: true" in error["message"]
    gr = error["congestion"]["global_route"]
    assert gr["total_overflow"] is None
    assert gr["overflow_per_layer"] is None
    assert len(gr["overflow_regions"]) == 20
    assert os.path.exists(gr["congestion_report_path"])
    reasons = error["congestion"]["null_reasons"]
    for key in (
        "total_overflow",
        "max_horizontal_overflow",
        "max_vertical_overflow",
        "usage_pct",
        "max_layer_usage_pct",
        "overflow_per_layer",
        "routed_net_count",
    ):
        assert f"global_route.{key}" in reasons
    assert "global_route.overflow_regions" not in reasons


def test_cli_grt_0116_neither_input_still_raises_the_routing_error(
    tmp_path, monkeypatch, capsys
):
    error = _run_cli_json(
        tmp_path, monkeypatch, capsys, route_stdout=_GRT_0116_ERROR_ONLY
    )

    assert "[ERROR GRT-0116]" in error["message"]
    gr = error["congestion"]["global_route"]
    assert gr["total_overflow"] is None
    assert gr["overflow_regions"] is None
    assert gr["congestion_report_path"] is None


def test_cli_unrelated_route_error_message_unchanged_with_null_congestion(
    tmp_path, monkeypatch, capsys
):
    error = _run_cli_json(
        tmp_path,
        monkeypatch,
        capsys,
        route_stdout="[ERROR DRT-0305] Net zero_ of signal type GROUND is not "
        "routable by TritonRoute. Move to special nets.\n",
    )

    assert error["message"].startswith(
        "openroad 'route' stage failed: [ERROR DRT-0305]"
    )
    gr = error["congestion"]["global_route"]
    assert gr["overflow_regions"] is None
    assert gr["congestion_report_path"] is None


def test_cli_reused_output_dir_ignores_prior_attempt_report(
    tmp_path, monkeypatch, capsys
):
    # First attempt: GRT-0116 after writing its congestion report -- the
    # diagnostic names that report's regions.
    error = _run_cli_json(
        tmp_path,
        monkeypatch,
        capsys,
        route_stdout=_GRT_0116_TABLE,
        report_text=_CONGESTION_REPORT_FIXTURE.read_text(encoding="utf-8"),
    )
    first_report_path = error["congestion"]["global_route"]["congestion_report_path"]
    assert first_report_path is not None and os.path.isfile(first_report_path)
    assert error["congestion"]["global_route"]["overflow_regions"] is not None

    # Second attempt in the same (default-root) output directory fails with
    # the table printed but never rewriting that report: the prior attempt's
    # file is cleared before OpenROAD launches and never named as evidence.
    error = _run_cli_json(tmp_path, monkeypatch, capsys, route_stdout=_GRT_0116_TABLE)

    assert not os.path.exists(first_report_path)
    gr = error["congestion"]["global_route"]
    assert gr["total_overflow"] == 12  # stdout evidence is this attempt's
    assert gr["overflow_regions"] is None
    assert gr["overflow_by_region"] is None
    assert gr["congestion_report_path"] is None
    reasons = error["congestion"]["null_reasons"]
    assert "route exited before" in reasons["global_route.congestion_report_path"]
    for key in (
        "global_route.overflow_regions",
        "global_route.overflow_by_region",
    ):
        assert "no `global_route -congestion_report_file` report" in reasons[key]


def test_route_failure_congestion_is_none_off_the_route_stage(tmp_path):
    from types import SimpleNamespace

    completed = SimpleNamespace(stdout="", engine_log={})
    assert (
        route_failure_congestion("place", completed, str(tmp_path), "gcd", []) is None
    )
