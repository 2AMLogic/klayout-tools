"""Offline regressions for #2089; no live OpenROAD run is claimed.

The Metal Spacing record below is the verbatim reporter-provided excerpt in
https://github.com/2AMLogic/klayout-tools/issues/2089. Other records and the
append-style engine are synthetic variations to test distinct sites and passes.
"""

import json
import shlex
from pathlib import Path

import pytest

from klayout_tools import place_and_route, place_and_route_reports
from test_place_and_route import (
    _script_lines,
    _setup_success_env,
    _stage_script,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

_REPORTED_RECORD = (
    "violation type: Metal Spacing\n"
    "\tsrcs: net:_04497_ net:VSS\n"
    "\tbbox = (23.4600, 192.4620) - (23.6600, 192.7300) on Layer Metal2\n"
)


@pytest.mark.parametrize(
    "other",
    [
        _REPORTED_RECORD.replace("Metal Spacing", "Metal Short"),
        _REPORTED_RECORD.replace("_04497_", "_04498_"),
        _REPORTED_RECORD.replace("23.4600", "23.4500"),
        _REPORTED_RECORD.replace("Metal2", "Metal3"),
    ],
)
def test_route_count_deduplicates_records_without_merging_distinct_sites(
    tmp_path, other
):
    report = tmp_path / "route.rpt"
    report.write_text(_REPORTED_RECORD * 2 + other + _REPORTED_RECORD + other)
    assert place_and_route._count_route_drc_violations(str(report)) == 2


def test_incomplete_route_records_are_not_deduplicated(tmp_path):
    report = tmp_path / "route.rpt"
    report.write_text("violation type: Metal Spacing\n" * 2)
    assert place_and_route._count_route_drc_violations(str(report)) == 2


def _append_route_outputs(lines, metrics_path, final_records):
    """Execute only artifact/metrics effects of routing commands in order.

    Seed stale outputs first, then append as the affected engine does. Each
    earlier pass has a different unresolved site, so deduplication alone cannot
    make the final count right when a stale pass leaks into the final report.
    """
    calls = [shlex.split(line) for line in lines if line.startswith("detailed_route ")]
    for call in calls:
        Path(call[2]).write_text(_REPORTED_RECORD.replace("_04497_", "stale"))
        Path(call[4]).write_text("stale maze\n")
    metric_pairs = []
    stage_format = "{}"
    pass_index = 0
    for line in lines:
        words = shlex.split(line)
        if line.startswith("file delete -force "):
            for path in words[3:]:
                Path(path).unlink(missing_ok=True)
        elif line.startswith("utl::push_metrics_stage "):
            stage_format = words[1]
        elif line == "utl::pop_metrics_stage":
            stage_format = "{}"
        elif line.startswith("detailed_route "):
            is_final = pass_index == len(calls) - 1
            records = (
                final_records
                if is_final
                else [_REPORTED_RECORD.replace("_04497_", f"pass{pass_index}")]
            )
            with Path(words[2]).open("a") as report:
                report.write("".join(record * 2 for record in records))
            with Path(words[4]).open("a") as maze:
                maze.write(f"pass {pass_index}\n")
            # Duplicate iteration keys can exist even within a pass; keep their
            # values inspectable instead of silently discarding the earlier one.
            metric_pairs.extend(
                (stage_format.replace("{}", key), value)
                for key, value in [
                    ("route__drc_errors__iter:1", 91),
                    ("route__drc_errors__iter:1", 2 * len(records)),
                    ("route__drc_errors", len(records)),
                    ("route__drc_errors", 2 * len(records)),
                    ("route__wirelength", 100 + pass_index),
                ]
            )
            pass_index += 1
    # Keep other stage metrics intact, append the engine's duplicate pairs.
    metrics = json.loads(Path(metrics_path).read_text())
    metrics.pop("route__drc_errors", None)
    metrics.pop("route__wirelength", None)
    pairs = [*metrics.items(), *metric_pairs]
    Path(metrics_path).write_text(
        "{" + ",".join(f"{json.dumps(k)}:{json.dumps(v)}" for k, v in pairs) + "}"
    )
    return calls


@pytest.mark.parametrize("repairs", [0, 1, 3])
@pytest.mark.parametrize("final_count", [0, 2])
def test_final_route_artifacts_and_metrics_are_authoritative(
    tmp_path, monkeypatch, repairs, final_count
):
    request = _setup_success_env(
        tmp_path, monkeypatch, max_antenna_repair_iterations=repairs
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    base_run = place_and_route.subprocess.run
    route_calls = []
    final_records = [_REPORTED_RECORD, _REPORTED_RECORD.replace("Metal2", "Metal3")][
        :final_count
    ]

    def append_style_run(cmd, **kwargs):
        completed = base_run(cmd, **kwargs)
        if len(cmd) > 5 and cmd[5].endswith("_route.tcl"):
            route_calls[:] = _append_route_outputs(
                _script_lines(cmd[5]), cmd[4], final_records
            )
        return completed

    monkeypatch.setattr(place_and_route.subprocess, "run", append_style_run)
    result = place_and_route.run_place_and_route(request)
    assert result["route_drc_violation_count"] == final_count
    assert result["stages"][-1]["route_drc_violation_count"] == final_count
    assert result["wirelength_um"] == 100 + repairs
    assert len(route_calls) == repairs + 1
    assert len({call[2] for call in route_calls}) == repairs + 1
    assert len({call[4] for call in route_calls}) == repairs + 1
    for pass_index, call in enumerate(route_calls):
        assert Path(call[4]).read_text() == f"pass {pass_index}\n"
    assert Path(route_calls[-1][2]).name == "gcd_route_drc.rpt"
    metrics_path = Path(_stage_script(request, "route")).with_name(
        "gcd_route_metrics.json"
    )

    def reject_duplicates(pairs):
        keys = [key for key, _ in pairs]
        assert len(keys) == len(set(keys))
        return dict(pairs)

    metrics = json.loads(metrics_path.read_text(), object_pairs_hook=reject_duplicates)
    assert metrics["route__drc_errors"] == final_count
    assert metrics["klt__repeated_metrics"]["route__drc_errors"] == [
        final_count,
        2 * final_count,
    ]
    assert metrics["klt__repeated_metrics"]["route__drc_errors__iter:1"] == [
        91,
        2 * final_count,
    ]
    for pass_index in range(repairs):
        assert metrics[f"route_pass:{pass_index}__route__drc_errors"] == 2
        assert (
            metrics[f"route_pass:{pass_index}__route__wirelength"] == 100 + pass_index
        )


def test_missing_final_report_does_not_fall_back_to_engine_or_earlier_pass(
    tmp_path, monkeypatch
):
    request = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, route_drc_violations=5)
    _stub_merge_def_to_gds(monkeypatch)
    base_run = place_and_route.subprocess.run

    def missing_report_run(cmd, **kwargs):
        completed = base_run(cmd, **kwargs)
        if len(cmd) > 5 and cmd[5].endswith("_route.tcl"):
            calls = [
                shlex.split(line)
                for line in _script_lines(cmd[5])
                if line.startswith("detailed_route ")
            ]
            Path(calls[0][2]).write_text(_REPORTED_RECORD)
            Path(calls[-1][2]).unlink()
        return completed

    monkeypatch.setattr(place_and_route.subprocess, "run", missing_report_run)
    result = place_and_route.run_place_and_route(request)
    assert result["route_drc_violation_count"] is None
    metrics_path = Path(_stage_script(request, "route")).with_name(
        "gcd_route_metrics.json"
    )
    assert json.loads(metrics_path.read_text())["route__drc_errors"] is None


def test_unreadable_route_report_is_unknown(tmp_path):
    report = tmp_path / "route.rpt"
    report.write_bytes(b"\xff\xfe")
    assert place_and_route._count_route_drc_violations(str(report)) is None


def test_final_metrics_replace_read_only_engine_artifact(tmp_path):
    metrics_path = tmp_path / "route_metrics.json"
    metrics_path.write_text('{"route__drc_errors": 46}\n')
    # Docker OpenROAD writes a root-owned 0644 inode in a user-owned
    # directory. Removing our own write permission reproduces that boundary.
    metrics_path.chmod(0o444)
    place_and_route_reports.write_route_metrics(
        str(metrics_path), {"route__wirelength": 42}, 23, error_cls=RuntimeError
    )
    assert json.loads(metrics_path.read_text()) == {
        "route__wirelength": 42,
        "route__drc_errors": 23,
    }
    assert list(tmp_path.iterdir()) == [metrics_path]


def test_failed_metrics_publication_preserves_engine_artifact(tmp_path, monkeypatch):
    metrics_path = tmp_path / "route_metrics.json"
    original = '{"route__drc_errors": 46}\n'
    metrics_path.write_text(original)

    def fail_replace(*args):
        raise OSError("publication denied")

    monkeypatch.setattr(place_and_route_reports.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="publication denied"):
        place_and_route_reports.write_route_metrics(
            str(metrics_path), {}, 23, error_cls=RuntimeError
        )
    assert metrics_path.read_text() == original
    assert list(tmp_path.iterdir()) == [metrics_path]
