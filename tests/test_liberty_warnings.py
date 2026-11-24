"""Tests for ``klayout_tools._liberty_warnings`` and its wiring into
``klt sta`` / ``klt place-and-route`` (issue #7411).

OpenSTA's Liberty reader warns and *discards a timing model* when a table
names an undefined ``lu_table_template`` (``STA-1253``/``STA-1311``), so
the reported slacks silently omit the paths it would have launched. These
tests pin: both codes counted (unrelated ``[WARNING STA-*]`` not);
identical lines deduplicated across P&R stage re-reads; ``klt sta``'s
always-present per-corner ``liberty_warnings`` block attributed to its
own corner; ``klt place-and-route``'s single ``warnings[]`` entry (none on
a clean deck); ``status``/``timing_status``/exit codes unchanged. Canned
lines from the issue's reproduction (OpenROAD ``26Q3-1510-g6cb3f2b704``);
engine stubs are local so no shared suite fixture grows for one feature.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import (
    _liberty_warnings,
    place_and_route,
    place_and_route_warnings,
    post_route_sta,
)
from klayout_tools.cli import sta_cmd
from klayout_tools.place_and_route import run_place_and_route
from klayout_tools.post_route_sta import run_sta

# Fixture helpers from the sibling suites (the repo's established
# cross-test-import convention -- see tests/test_array_check_lane_extent.py).
from test_place_and_route import (
    _BASE_STRAPS,
    _fake_def,
)
from test_place_and_route import (
    _setup_success_env as _setup_pnr_env,
)
from test_place_and_route import (
    _stub_openroad_success as _stub_pnr_openroad,
)
from test_post_route_sta import _setup_multi_corner_env, _setup_success_env

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


#: Two verbatim Liberty-reader warnings from the issue's reproduction --
#: a timing table naming the undefined ``delay_template_7x7`` (STA-1253)
#: and the model-less timing group it leaves behind (STA-1311).
_STA_1253 = (
    "[WARNING STA-1253] /pdk/install/lib.lib line 4021, "
    "table_template delay_template_7x7 not found."
)
_STA_1311 = (
    "[WARNING STA-1311] /pdk/install/lib.lib line 4021, "
    "no table models found in timing group."
)


def _env_deck(tmp_path) -> Path:
    """The deck `_setup_success_env` resolves for the nominal corner --
    the absolute engine path a real warning line quotes."""
    return Path(
        tmp_path,
        "install/sky130A/libs.ref/sky130_fd_sc_hd/lib/"
        "sky130_fd_sc_hd__tt_025C_1v80.lib",
    )


def _env_warning_lines(tmp_path) -> list[str]:
    deck = _env_deck(tmp_path)
    return [
        f"[WARNING STA-1253] {deck} line 4021, table_template "
        "delay_template_7x7 not found.",
        f"[WARNING STA-1311] {deck} line 4021, no table models found in timing group.",
    ]


def _scrubbed_env_warnings(tmp_path) -> list[str]:
    """The persisted spelling: the deck lives outside any repo here, so
    #2879's rule rewrites it to ``<outside repo>``."""
    return [
        line.replace(str(_env_deck(tmp_path)), "<outside repo>")
        for line in _env_warning_lines(tmp_path)
    ]


# --- Parser / formatter unit tests. ---


def test_parse_counts_both_codes_with_by_code_and_sample():
    block = _liberty_warnings.parse_liberty_model_warnings(
        f"[INFO STA-0000] some info line\n{_STA_1253}\n{_STA_1311}\n"
    )

    assert block["count"] == 2
    assert block["by_code"] == {"STA-1253": 1, "STA-1311": 1}
    assert block["sample"] == [_STA_1253, _STA_1311]
    assert block["truncated"] is False


def test_parse_ignores_unrelated_sta_codes():
    """Only the two codes that mean *a model was dropped* count (the
    curator's rejected alternative: scanning every STA-* would be noise)."""
    stdout = "\n".join(
        [
            _STA_1253,
            "[WARNING STA-1650] /tmp/top.spef line 16, net n1 not found.",
            "[WARNING STA-0198] some other liberty-reader complaint.",
            "[WARNING STA-9999] a future unknown code.",
            _STA_1311,
        ]
    )

    block = _liberty_warnings.parse_liberty_model_warnings(stdout)

    assert block["count"] == 2
    assert block["by_code"] == {"STA-1253": 1, "STA-1311": 1}
    assert all(
        "STA-16" not in line and "STA-01" not in line for line in block["sample"]
    )


def test_parse_dedupes_identical_lines():
    doubled = "\n".join([_STA_1253, _STA_1253, _STA_1311, _STA_1311])

    block = _liberty_warnings.parse_liberty_model_warnings(doubled)

    assert block["count"] == 2
    assert block["by_code"] == {"STA-1253": 1, "STA-1311": 1}


def test_parse_reads_stderr_only():
    """OpenROAD builds differ in which stream the logger writes to, so a
    stderr-only warning still counts."""
    block = _liberty_warnings.parse_liberty_model_warnings("", f"{_STA_1253}\n")

    assert block["count"] == 1
    assert block["by_code"]["STA-1253"] == 1


def test_parse_clean_run_is_always_present_zero_block():
    block = _liberty_warnings.parse_liberty_model_warnings("clean run\n")

    assert block == {
        "count": 0,
        "by_code": {"STA-1253": 0, "STA-1311": 0},
        "sample": [],
        "sample_limit": _liberty_warnings.LIBERTY_MODEL_WARNING_SAMPLE_LIMIT,
        "truncated": False,
    }


def test_parse_sample_is_capped_but_count_is_not():
    warnings = [
        f"[WARNING STA-1253] /pdk/lib.lib line {line}, table_template t{i} not found."
        for i, line in enumerate(range(5000, 5050))
    ]

    block = _liberty_warnings.parse_liberty_model_warnings("\n".join(warnings))

    assert block["count"] == 50
    assert block["by_code"]["STA-1253"] == 50
    limit = _liberty_warnings.LIBERTY_MODEL_WARNING_SAMPLE_LIMIT
    assert len(block["sample"]) == limit
    assert block["truncated"] is True


def test_merge_dedupes_across_stream_pairs():
    """The P&R accumulator: stage re-reads of the same deck count once."""
    merged = _liberty_warnings.merge_liberty_model_warning_lines(
        None, f"{_STA_1253}\n{_STA_1311}\n"
    )
    merged = _liberty_warnings.merge_liberty_model_warning_lines(
        merged, f"{_STA_1253}\n{_STA_1311}\n"
    )
    merged = _liberty_warnings.merge_liberty_model_warning_lines(
        merged, f"{_STA_1253.replace('line 4021', 'line 4099')}\n"
    )

    block = _liberty_warnings.liberty_model_warning_block(merged)

    # A different line number is a different (genuinely distinct) warning;
    # the byte-identical repeats across stages are one.
    assert block["count"] == 3
    assert block["by_code"] == {"STA-1253": 2, "STA-1311": 1}


def test_format_returns_none_for_clean_block():
    block = _liberty_warnings.parse_liberty_model_warnings("")

    assert _liberty_warnings.format_liberty_model_warning(block) is None


def test_format_names_codes_and_counts_conditionally():
    block = _liberty_warnings.parse_liberty_model_warnings(
        f"{_STA_1253}\n{_STA_1311}\n"
    )

    message = _liberty_warnings.format_liberty_model_warning(block)

    assert message is not None
    assert "STA-1253 x 1" in message
    assert "STA-1311 x 1" in message
    # Conditional wording per the champion review.
    assert "may omit" in message


# --- Engine stubs (local: the sibling suites' shared fakes stay untouched). ---


_STA_METRICS = {
    "timing__setup__ws": -0.15,
    "timing__setup__tns": -1.2,
    "timing__hold__ws": 0.03812,
    "timing__hold__tns": 0.0,
    "timing__fmax": 500_000_000.0,
    "power__total": 0.0084,
    "clock__skew__setup": 0.021,
}


def _stub_sta_openroad(
    monkeypatch,
    *,
    liberty_stdout: list[str] | None = None,
    liberty_stderr: list[str] | None = None,
    version: str = "26Q3-771-gdeadbeef",
) -> None:
    """One `klt sta` session: metrics dump + empty violation blocks, plus
    issue #7411's warning lines on either stream."""

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout=f"{version} \n")
        metrics_path = cmd[4]
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(_STA_METRICS, handle)
        return fake_completed(
            returncode=0,
            stdout="\n".join([*(liberty_stdout or []), *_violation_blocks()]),
            stderr="\n".join(liberty_stderr or []),
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)


def _stub_sta_multi_corner(
    monkeypatch,
    liberty_by_corner: dict[str, list[str]],
    *,
    corners: tuple[str, ...] = ("tt_025C_1v80", "ss_100C_1v60"),
    version: str = "26Q3-771-gdeadbeef",
) -> None:
    """One session per corner, keyed by the corner name in the generated
    script's own `read_liberty` line (the real sessions' attribution
    mechanism). Corners absent from `liberty_by_corner` get a clean deck."""

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout=f"{version} \n")
        metrics_path, script_path = cmd[4], cmd[5]
        script_text = Path(script_path).read_text(encoding="utf-8")
        corner_name = next(name for name in corners if f"__{name}.lib" in script_text)
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(_STA_METRICS, handle)
        return fake_completed(
            returncode=0,
            stdout="\n".join(
                [*liberty_by_corner.get(corner_name, []), *_violation_blocks()]
            ),
        )

    monkeypatch.setattr(post_route_sta.subprocess, "run", fake_run)


def _violation_blocks() -> list[str]:
    return [
        post_route_sta._SETUP_VIOLATIONS_BEGIN,
        post_route_sta._SETUP_VIOLATIONS_END,
        post_route_sta._HOLD_VIOLATIONS_BEGIN,
        post_route_sta._HOLD_VIOLATIONS_END,
    ]


def _stub_pnr_with_liberty_warnings(
    monkeypatch, lines: list[str], **stub_kwargs
) -> None:
    """Wrap the shared P&R fake so every stage's stdout carries the
    warning lines, exactly as a real run produces them."""
    _stub_pnr_openroad(monkeypatch, **stub_kwargs)
    inner = subprocess.run

    def fake_run(cmd, **kwargs):
        result = inner(cmd, **kwargs)
        if cmd[:2] == ["openroad", "-version"]:
            return result
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout="\n".join([*lines, result.stdout]),
            stderr=result.stderr,
        )

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


# --- `klt sta` wiring. ---


def test_run_sta_surfaces_liberty_warnings(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch, liberty_stdout=_env_warning_lines(tmp_path))

    report = run_sta(request_path)

    assert report["status"] == "ok"
    assert report["timing_status"] == "constrained"
    assert report["liberty_warnings"]["count"] == 2
    assert report["liberty_warnings"]["by_code"] == {"STA-1253": 1, "STA-1311": 1}
    assert report["liberty_warnings"]["sample"] == _scrubbed_env_warnings(tmp_path)


def test_run_sta_clean_liberty_reports_zero_block(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch)

    report = run_sta(request_path)

    assert report["liberty_warnings"]["count"] == 0
    assert report["liberty_warnings"]["by_code"] == {"STA-1253": 0, "STA-1311": 0}


def test_run_sta_stderr_only_liberty_warnings_counted(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch, liberty_stderr=_env_warning_lines(tmp_path)[:1])

    report = run_sta(request_path)

    assert report["liberty_warnings"]["count"] == 1
    assert report["liberty_warnings"]["by_code"]["STA-1253"] == 1


def test_run_sta_liberty_warning_sample_scrubs_absolute_deck_path(
    tmp_path, monkeypatch
):
    """#2879's rule, extended to the deck: the verbatim sample carries the
    deck's report identity, never the absolute host path."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch, liberty_stdout=_env_warning_lines(tmp_path)[:1])

    report = run_sta(request_path)

    (sample_line,) = report["liberty_warnings"]["sample"]
    assert str(_env_deck(tmp_path)) not in sample_line
    assert str(_env_deck(tmp_path).parent) not in sample_line
    assert "outside repo" in sample_line


def test_run_sta_corners_attribute_warnings_to_their_own_corner(tmp_path, monkeypatch):
    """Per-corner attribution: a warning belongs to the corner whose own
    session (fresh deck read) produced it -- never to all corners."""
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_sta_multi_corner(monkeypatch, {"tt_025C_1v80": _env_warning_lines(tmp_path)})

    report = run_sta(request_path)

    by_corner = {entry["corner"]: entry for entry in report["corners"]}
    assert by_corner["tt_025C_1v80"]["liberty_warnings"]["count"] == 2
    assert by_corner["tt_025C_1v80"]["liberty_warnings"]["by_code"] == {
        "STA-1253": 1,
        "STA-1311": 1,
    }
    assert by_corner["ss_100C_1v60"]["liberty_warnings"]["count"] == 0


def test_sta_text_output_prints_liberty_warning_line(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch, liberty_stdout=_env_warning_lines(tmp_path))

    sta_cmd._print_text(run_sta(request_path))

    assert (
        "liberty_warnings: count=2 (STA-1253=1, STA-1311=1)" in capsys.readouterr().out
    )


def test_sta_text_output_silent_on_clean_deck(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_sta_openroad(monkeypatch)

    sta_cmd._print_text(run_sta(request_path))

    assert "liberty_warnings" not in capsys.readouterr().out


# --- `klt place-and-route` wiring. ---


def _setup_clean_warnings_pnr_env(tmp_path, monkeypatch) -> str:
    """A `place`-stage request with complete measured power delivery, so
    `warnings` is `[]` before #7411's producer (the clean baseline)."""
    return _setup_pnr_env(
        tmp_path,
        monkeypatch,
        target_stage="place",
        power={
            "power_net": "VPWR",
            "ground_net": "VGND",
            "straps": _BASE_STRAPS,
        },
    )


def _complete_power_def() -> str:
    return _fake_def(
        masters={
            "sky130_fd_sc_hd__nand2_1": 4,
            "sky130_fd_sc_hd__tapvpwrvgnd_1": 3,
        },
        special_nets=(("VPWR", 6, 4, 2), ("VGND", 6, 4, 2)),
    )


def test_place_and_route_warns_once_for_liberty_model_warnings(tmp_path, monkeypatch):
    request_path = _setup_clean_warnings_pnr_env(tmp_path, monkeypatch)
    _stub_pnr_with_liberty_warnings(
        monkeypatch,
        _env_warning_lines(tmp_path),
        stages=("floorplan", "place"),
        def_text=_complete_power_def(),
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert len(report["warnings"]) == 1
    (liberty,) = report["warnings"]
    assert "STA-1253 x 1" in liberty
    assert "STA-1311 x 1" in liberty


def test_place_and_route_clean_liberty_adds_no_warning(tmp_path, monkeypatch):
    request_path = _setup_clean_warnings_pnr_env(tmp_path, monkeypatch)
    _stub_pnr_openroad(
        monkeypatch, stages=("floorplan", "place"), def_text=_complete_power_def()
    )

    report = run_place_and_route(request_path)

    assert report["warnings"] == []


def test_place_and_route_repeated_warning_across_stages_counted_once(
    tmp_path, monkeypatch
):
    """Identical lines come back once per stage -- count them once."""
    request_path = _setup_clean_warnings_pnr_env(tmp_path, monkeypatch)
    _stub_pnr_with_liberty_warnings(
        monkeypatch,
        _env_warning_lines(tmp_path),
        stages=("floorplan", "place"),
        def_text=_complete_power_def(),
    )

    report = run_place_and_route(request_path)

    (liberty,) = report["warnings"]
    assert "STA-1253 x 1" in liberty
    assert "STA-1311 x 1" in liberty


def test_liberty_warning_lines_from_resumed_recovers_retained_logs(tmp_path):
    """A `--resume`d stage's Liberty read survives only in the retained
    `openroad-logs` -- read both streams, skip silently when gone."""
    logs_root = tmp_path / "openroad-logs" / "abc123"
    logs_root.mkdir(parents=True)
    (logs_root / "stdout.log").write_text(f"{_STA_1253}\n", encoding="utf-8")
    (logs_root / "stderr.log").write_text(f"{_STA_1311}\n", encoding="utf-8")
    records = [{"invocation_id": "abc123"}, {"invocation_id": "gone"}]

    merged = place_and_route_warnings.liberty_warning_lines_from_resumed(
        [], records, str(tmp_path)
    )

    assert merged == [_STA_1253.strip(), _STA_1311.strip()]
    # no records / no invocation id: nothing recovered, never an error
    assert (
        place_and_route_warnings.liberty_warning_lines_from_resumed(
            [], [{"no": "invocation"}], str(tmp_path)
        )
        == []
    )
