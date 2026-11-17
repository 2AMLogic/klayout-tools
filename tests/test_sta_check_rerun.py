"""Tests for ``klt sta --check`` / ``--rerun`` (issue #5841).

Cheap mode re-hashes the request's geometry source and liberty(ies) against a
committed report's recorded digests; full mode re-runs the analysis (OpenROAD
stubbed, as ``test_post_route_sta.py`` does) and diffs verdict-bearing fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import post_route_sta
from klayout_tools.cli import main
from klayout_tools.post_route_sta_verify import (
    _canonicalize_sta_report_for_rerun_diff,
)
from test_post_route_sta import (
    _base_request,
    _setup_multi_corner_env,
    _setup_success_env,
    _stub_openroad_multi_corner,
    _stub_openroad_success,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _commit_report(tmp_path: Path, capsys, request_path: str) -> str:
    """Run ``klt sta`` and commit its JSON report, as a downstream repo would."""
    assert main(["sta", request_path, "--format", "json"]) == 0
    path = tmp_path / "committed.json"
    path.write_text(capsys.readouterr().out, encoding="utf-8")
    return str(path)


def _verify(capsys, request_path: str, report_path: str, *flags: str):
    code = main(
        ["sta", request_path, "--check", report_path, "--format", "json", *flags]
    )
    return code, json.loads(capsys.readouterr().out)


def _edit_report(report_path: str, mutate) -> None:
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    mutate(data)
    Path(report_path).write_text(json.dumps(data), encoding="utf-8")


def _find_lib(tmp_path: Path, corner: str) -> Path:
    (lib,) = (tmp_path / "install").rglob(f"*__{corner}.lib")
    return lib


# --------------------------------------------------------------------------- #
# --check (cheap mode)
# --------------------------------------------------------------------------- #


def test_check_match_exits_zero(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)

    code, out = _verify(capsys, request_path, report)

    assert code == 0
    assert out["mode"] == "check"
    assert out["status"] == "match"
    assert [c["field"] for c in out["checks"]] == [
        "provenance.input.content_hash",
        "provenance.deck.content_hash",
    ]
    assert all(c["match"] for c in out["checks"])


def test_check_does_not_run_openroad(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)

    def boom(*args, **kwargs):
        raise AssertionError("--check must not invoke openroad")

    monkeypatch.setattr(post_route_sta.subprocess, "run", boom)

    code, out = _verify(capsys, request_path, report)

    assert code == 0 and out["status"] == "match"


def test_check_drifted_geometry_names_input_hash(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    (tmp_path / "top.def").write_text("# edited def\n", encoding="utf-8")

    code, out = _verify(capsys, request_path, report)

    assert code == 3
    assert out["status"] == "drifted"
    moved = [c["field"] for c in out["checks"] if not c["match"]]
    assert moved == ["provenance.input.content_hash"]


def test_check_drifted_liberty_names_deck_hash(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    with _find_lib(tmp_path, "tt_025C_1v80").open("a", encoding="utf-8") as handle:
        handle.write("/* edited */\n")

    code, out = _verify(capsys, request_path, report)

    assert code == 3
    moved = [c["field"] for c in out["checks"] if not c["match"]]
    assert moved == ["provenance.deck.content_hash"]


def test_check_verilog_mode_without_def(tmp_path, monkeypatch, capsys):
    _setup_success_env(tmp_path, monkeypatch)
    (tmp_path / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    request = _base_request(verilog="top.v", hdl_toplevel="top")
    del request["def"]
    request_path = _write_request(tmp_path / "request.json", request)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)

    code, out = _verify(capsys, request_path, report)
    assert code == 0 and out["status"] == "match"

    (tmp_path / "top.v").write_text("module top; wire x; endmodule\n", encoding="utf-8")
    code, out = _verify(capsys, request_path, report)
    assert code == 3
    assert [c["field"] for c in out["checks"] if not c["match"]] == [
        "provenance.input.content_hash"
    ]


def test_check_multi_corner_names_the_moved_corner(tmp_path, monkeypatch, capsys):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)

    code, out = _verify(capsys, request_path, report)
    assert code == 0
    assert [c["field"] for c in out["checks"]] == [
        "provenance.input.content_hash",
        "corners[tt_025C_1v80].deck.content_hash",
        "corners[ss_100C_1v60].deck.content_hash",
    ]

    with _find_lib(tmp_path, "ss_100C_1v60").open("a", encoding="utf-8") as handle:
        handle.write("/* edited */\n")
    code, out = _verify(capsys, request_path, report)
    assert code == 3
    assert [c["field"] for c in out["checks"] if not c["match"]] == [
        "corners[ss_100C_1v60].deck.content_hash"
    ]


def test_check_null_recorded_hash_never_matches(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    _edit_report(
        report, lambda d: d["provenance"]["input"].__setitem__("content_hash", None)
    )

    code, out = _verify(capsys, request_path, report)

    assert code == 3
    assert out["checks"][0]["match"] is False


def test_check_multi_corner_missing_corner_entry_never_matches(
    tmp_path, monkeypatch, capsys
):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    _edit_report(report, lambda d: d["corners"].pop())

    code, out = _verify(capsys, request_path, report)

    assert code == 3
    assert [c["field"] for c in out["checks"] if not c["match"]] == [
        "corners[ss_100C_1v60].deck.content_hash"
    ]


def test_check_missing_or_unparseable_report_is_a_clean_error(
    tmp_path, monkeypatch, capsys
):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    assert main(["sta", request_path, "--check", str(tmp_path / "nope.json")]) == 1
    assert "committed report not found" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["sta", request_path, "--check", str(bad)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# --rerun (full mode)
# --------------------------------------------------------------------------- #


def test_rerun_without_check_is_exit_one(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)

    assert main(["sta", request_path, "--rerun"]) == 1
    assert "--rerun requires --check" in capsys.readouterr().err


def test_rerun_verifies_clean_despite_per_run_bookkeeping(
    tmp_path, monkeypatch, capsys
):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, version="26Q3-771-gdeadbeef")
    report = _commit_report(tmp_path, capsys, request_path)
    committed = json.loads(Path(report).read_text(encoding="utf-8"))

    # The next run gets a new engine_log.invocation_id by construction; make
    # the version echoes differ too (a newer openroad, a newer klayout).
    _stub_openroad_success(monkeypatch, version="26Q4-1-gcafef00d")
    _edit_report(
        report,
        lambda d: d["provenance"].__setitem__("klayout_version", "0.30.0-old"),
    )

    code, out = _verify(capsys, request_path, report, "--rerun")

    assert code == 0
    assert out["mode"] == "rerun" and out["status"] == "match"
    assert out["drift"] == []
    assert (
        out["fresh"]["engine_log"]["invocation_id"]
        != committed["engine_log"]["invocation_id"]
    )
    assert out["fresh"]["engine_version"] != committed["engine_version"]


def test_rerun_multi_corner_verifies_clean(tmp_path, monkeypatch, capsys):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)

    code, out = _verify(capsys, request_path, report, "--rerun")

    assert code == 0 and out["drift"] == []


def test_rerun_doctored_report_never_false_passes(tmp_path, monkeypatch, capsys):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    _edit_report(report, lambda d: d.__setitem__("worst_slack_ns", 9.99))

    code, out = _verify(capsys, request_path, report, "--rerun")

    assert code == 3
    assert out["status"] == "drifted"
    assert [d["field"] for d in out["drift"]] == ["worst_slack_ns"]


def test_rerun_doctored_multi_corner_names_corner_field(tmp_path, monkeypatch, capsys):
    request_path = _setup_multi_corner_env(tmp_path, monkeypatch)
    _stub_openroad_multi_corner(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    _edit_report(report, lambda d: d["corners"][1].__setitem__("worst_slack_ns", 1.0))

    code, out = _verify(capsys, request_path, report, "--rerun")

    assert code == 3
    assert [d["field"] for d in out["drift"]] == ["corners.1.worst_slack_ns"]


def test_rerun_input_path_envelope_is_compared(tmp_path, monkeypatch, capsys):
    """Both sides carry the same `{path, scope}` envelope shape, so a path
    that really changed is drift -- not silently normalized away."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    report = _commit_report(tmp_path, capsys, request_path)
    _edit_report(
        report,
        lambda d: d["def_path"].update({"path": "other/top.def", "scope": "repo"}),
    )

    code, out = _verify(capsys, request_path, report, "--rerun")

    assert code == 3
    assert [d["field"] for d in out["drift"]] == ["def_path.path", "def_path.scope"]


def test_rerun_canonicalizer_drops_only_run_scoped_bookkeeping():
    sdf = {"path": "a.sdf", "scope": "repo", "content_hash": "sha256:x"}
    report = {
        "engine_log": {"invocation_id": "u1"},
        "sdf": dict(sdf),
        "corners": [
            {"corner": "tt", "engine_log": {"invocation_id": "u2"}, "sdf": dict(sdf)},
            {"corner": "ss", "worst_slack_ns": -1.0, "sdf": None},
        ],
    }

    assert _canonicalize_sta_report_for_rerun_diff(report) == {
        "sdf": {"path": "a.sdf", "scope": "repo"},
        "corners": [
            {"corner": "tt", "sdf": {"path": "a.sdf", "scope": "repo"}},
            {"corner": "ss", "worst_slack_ns": -1.0, "sdf": None},
        ],
    }
