"""Tests for the `save_all_supersedes` diagnostic (issue #5622).

Split from `tests/test_sim_save_mode.py` (pinned at its
`file-size-baseline.json` ceiling) when the diagnostic moved into
`sim_save_diagnostic.py`; shared helpers are imported from the sibling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_batch, sim_remote, sim_save_diagnostic
from test_sim_save_mode import _capture_decks, _write_request

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

# --------------------------------------------------------------------------- #
# Issue #5622: warn when the generated `save all` supersedes a body `.save`
# --------------------------------------------------------------------------- #

_SAVE_BODY = ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\n.save v(vdd) v(out)\n"
_TRAN = {"kind": "tran", "args": "1n 1u"}


def _supersede(report: dict) -> list[dict]:
    return [
        d for d in report.get("diagnostics", []) if d["code"] == "save_all_supersedes"
    ]


def _run_save_request(tmp_path, monkeypatch, *, body=_SAVE_BODY, **overrides):
    (tmp_path / "body.spice").write_text(body)
    options = overrides.pop("options", {})
    request = {"netlist": "body.spice", "analysis": _TRAN, "options": options}
    request.update(overrides)
    path = _write_request(tmp_path, request)
    _capture_decks(monkeypatch)
    return sim.run_sim(str(path))


@pytest.mark.parametrize(
    ("options", "warns"),
    [
        ({"waveforms": True}, True),
        ({"waveforms": True, "save_mode": "all"}, True),
        ({"waveforms": True, "save_mode": "netlist"}, False),
        ({"waveforms": True, "save_vectors": ["v(out)"]}, False),
        ({"waveforms": False}, False),
        ({}, False),
    ],
)
def test_supersede_warning_gating_matrix(tmp_path, monkeypatch, options, warns):
    report = _run_save_request(tmp_path, monkeypatch, options=options)
    found = _supersede(report)
    assert bool(found) is warns
    assert len(found) <= 1


def test_supersede_warning_without_body_save_is_absent(tmp_path, monkeypatch):
    report = _run_save_request(
        tmp_path,
        monkeypatch,
        body=".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\n",
        options={"waveforms": True},
    )
    assert _supersede(report) == []


def test_supersede_warning_names_both_facts_and_remedies(tmp_path, monkeypatch):
    report = _run_save_request(tmp_path, monkeypatch, options={"waveforms": True})
    (diag,) = _supersede(report)
    assert diag["severity"] == "warning"
    message = diag["message"]
    assert "restricts the saved signal set" in message
    assert ".save v(vdd) v(out)" in message
    assert "widens it" in message and "save all" in message
    assert 'options.save_mode to "netlist"' in message
    assert "options.save_vectors" in message
    assert "save_all_supersedes" in sim.DIAGNOSTIC_CODES


def test_supersede_warning_does_not_fail_a_run_under_fail_on_diagnostic(
    tmp_path, monkeypatch
):
    """Request-level, so per-corner grading is untouched."""
    report = _run_save_request(
        tmp_path,
        monkeypatch,
        options={"waveforms": True, "fail_on_diagnostic": ["save_all_supersedes"]},
    )
    assert _supersede(report)
    assert all(c["status"] != "inconclusive" for c in report["corners"])


def test_supersede_warning_is_in_the_dry_run_report(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": _TRAN, "options": {"waveforms": True}},
    )
    decks = _capture_decks(monkeypatch)
    report = sim.run_sim(str(request), dry_run=True)
    assert report["dry_run"] is True
    assert len(_supersede(report)) == 1
    assert decks == []


@pytest.mark.parametrize(
    "body",
    [
        # `save` command inside .control is a different construct
        ".param vdd=1\nR1 a 0 1k\n.control\nsave v(a)\n.endc\n",
        # subcircuit-local card
        ".param vdd=1\nR1 a 0 1k\n.subckt inner x y\nR2 x y 1k\n.save v(x)\n.ends\n",
        # `.probe` does not restrict on ngspice-47
        ".param vdd=1\nR1 a 0 1k\n.probe v(a)\n",
        # `all`-only and bare cards restricted nothing to supersede
        ".param vdd=1\nR1 a 0 1k\n.save all\n",
        ".param vdd=1\nR1 a 0 1k\n.save\n",
        ".param vdd=1\nR1 a 0 1k\n.SAVE ALL\n",
    ],
    ids=[
        "control-block",
        "subckt",
        "probe",
        "save-all-card",
        "bare-save",
        "save-all-upper",
    ],
)
def test_supersede_warning_negative_constructs(tmp_path, monkeypatch, body):
    report = _run_save_request(
        tmp_path, monkeypatch, body=body, options={"waveforms": True}
    )
    assert _supersede(report) == []


@pytest.mark.parametrize(
    "body",
    [
        ".SAVE v(a)\nR1 a 0 1k\n",
        ".save v(a)\n.save v(b)\nR1 a 0 1k\n",
        ".save v(a)\n+ v(b)\nR1 a 0 1k\n",
        ".save all\n.save v(a)\nR1 a 0 1k\n",
    ],
    ids=[
        "uppercase",
        "several-cards-one-warning",
        "continuation",
        "all-beside-restricting",
    ],
)
def test_supersede_warning_edge_cases(tmp_path, monkeypatch, body):
    report = _run_save_request(
        tmp_path, monkeypatch, body=body, options={"waveforms": True}
    )
    assert len(_supersede(report)) == 1


def test_supersede_preflight_never_raises_on_an_unreadable_netlist(tmp_path):
    kwargs = {
        "engine": "ngspice",
        "save_mode": "all",
        "save_vectors": (),
        "want_waveforms": True,
    }
    assert sim_save_diagnostic.preflight(str(tmp_path / "missing"), **kwargs) is None
    binary = tmp_path / "bin.spice"
    binary.write_bytes(b"\xff\xfe\x00.save v(a)\n")
    sim_save_diagnostic.preflight(str(binary), **kwargs)  # must not raise


def test_raw_bytes_survives_batch_and_remote_artifact_rewrites(tmp_path):
    """`artifacts.raw_bytes` (issue #5622) is an integer inside the otherwise
    path-valued artifacts map; the batch install and the remote-path rewrite
    must skip it rather than treat it as a path (review of PR #6205: both
    walkers raised TypeError on the int after the artifacts were pulled)."""
    remote_root = "/job/outputs/artifacts"
    collected = tmp_path / "collected"
    worker_dir = collected / "artifacts" / "c0"
    worker_dir.mkdir(parents=True)
    (worker_dir / "raw.raw").write_bytes(b"raw")
    installed = tmp_path / "installed"
    corners = [
        # int first: the batch probe meets it before any path
        {
            "artifacts": {
                "raw_bytes": 3,
                "log": None,
                "raw": f"{remote_root}/c0/raw.raw",
            }
        },
        # realistic insertion order: int last, beside `raw`
        {
            "artifacts": {
                "log": None,
                "raw": f"{remote_root}/c0/raw.raw",
                "raw_bytes": 3,
            }
        },
    ]

    sim_batch._install_batch_artifacts(
        corners, collected_dir=str(collected), artifacts_dir=str(installed)
    )
    direct = {"artifacts": {"raw": f"{remote_root}/c0/raw.raw", "raw_bytes": 3}}
    sim_remote._rewrite_remote_artifact_paths(
        [direct], remote_root=remote_root, local_root=str(installed)
    )

    for corner in [*corners, direct]:
        assert corner["artifacts"]["raw_bytes"] == 3
        assert corner["artifacts"]["raw"] == str(installed / "c0" / "raw.raw")


def test_supersede_preflight_skips_xyce(tmp_path):
    path = tmp_path / "body.spice"
    path.write_text(_SAVE_BODY)
    assert (
        sim_save_diagnostic.preflight(
            str(path),
            engine="xyce",
            save_mode="all",
            save_vectors=(),
            want_waveforms=True,
        )
        is None
    )


def test_supersede_warning_does_not_change_the_emitted_deck(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": _TRAN, "options": {"waveforms": True}},
    )
    decks = _capture_decks(monkeypatch)
    sim.run_sim(str(request))
    assert decks and "save all" in decks[0]


def test_raw_bytes_reports_the_kept_rawfile_size(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": _TRAN,
            "options": {
                "waveforms": True,
                "keep_artifacts": True,
                "waveform_max_bytes": 1,
            },
        },
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = Path(cmd[cmd.index("-o") + 1])
        log_path.write_text("")
        (log_path.parent / "waveform.raw").write_text("x" * 123)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    artifacts = sim.run_sim(str(request))["corners"][0]["artifacts"]
    assert artifacts["raw_bytes"] == 123 == Path(artifacts["raw"]).stat().st_size


def test_raw_bytes_absent_when_no_rawfile_is_kept(tmp_path, monkeypatch):
    report = _run_save_request(tmp_path, monkeypatch, options={"waveforms": True})
    assert "raw_bytes" not in report["corners"][0]["artifacts"]
