"""A kept ``batch`` artifact tree is usable on the submitting host (#7905).

``options.keep_artifacts`` brings the job's ``artifacts/`` home. The kept
``corner.cir`` must not ``.include`` a path that only existed on the job
instance, and a corner whose engine log did not come back must say so rather
than report a bare ``artifacts.log: null``. Split from test_sim_batch.py to
keep it small.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_hints as sbh
from klayout_tools import sim_batch_kept_artifacts as kept
from test_sim_batch import (
    _FakeRunner,
    _run_failed_job,
    _sim_report,
    _status,
)

JOB_ROOT = "/var/tmp/eda-job-1"
JOB_NETLIST = f"{JOB_ROOT}/inputs/netlist.cir"
REMOTE = f"{JOB_ROOT}/outputs/artifacts"
CODE = sbh.ENGINE_LOG_NOT_RETAINED_CODE


def _deck(include: str = JOB_NETLIST) -> str:
    return (
        "* klt sim -- generated corner deck, do not edit\n"
        ".lib /pdk/sky130A/models/sky130.lib.spice tt\n"
        f".include {include}\n"
        ".temp 27\n"
    )


def _collected(tmp_path: Path, files: dict[str, str]) -> Path:
    """A fabricated ``outputs/`` tree holding ``artifacts/<name>``."""
    root = tmp_path / "collected"
    for name, text in files.items():
        path = root / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _corner(slug: str = "tt_27C", *, log=True, deck=True, **extra) -> dict:
    return {
        "corner_id": slug,
        "status": "pass",
        "diagnostics": [],
        "artifacts": {
            "log": f"{REMOTE}/{slug}/ngspice.log" if log else None,
            "raw": None,
            "waveform": None,
            "deck": f"{REMOTE}/{slug}/corner.cir" if deck else None,
        },
        **extra,
    }


def _install(tmp_path, corners, files, **kwargs):
    local = tmp_path / "local"
    sb._install_batch_artifacts(
        corners,
        collected_dir=str(_collected(tmp_path, files)),
        artifacts_dir=str(local),
        **kwargs,
    )
    return local


def _noted(corner: dict) -> list[dict]:
    return [d for d in corner["diagnostics"] if d["code"] == CODE]


# --- the deck's job-side .include -------------------------------------------


def test_install_points_the_deck_include_at_the_submitted_netlist(tmp_path):
    netlist = tmp_path / "tb.spice"
    netlist.write_text("R1 a b 1k\n")
    corners = [_corner()]
    local = _install(
        tmp_path,
        corners,
        {"tt_27C/corner.cir": _deck(), "tt_27C/ngspice.log": "log"},
        client_netlist=str(netlist),
    )

    text = (local / "tt_27C" / "corner.cir").read_text()
    assert "/var/tmp/eda-job" not in text
    (target,) = [
        line.split(None, 1)[1].strip()
        for line in text.splitlines()
        if line.startswith(".include")
    ]
    assert target == str(netlist) and Path(target).is_file()  # the include resolves
    # Only the netlist include moves: the worker's PDK `.lib` stays as written.
    assert ".lib /pdk/sky130A/models/sky130.lib.spice tt" in text
    assert ".temp 27" in text


def test_install_rewrites_expanded_netlist_and_spiceinit_that_name_the_path(tmp_path):
    corners = [_corner()]
    local = _install(
        tmp_path,
        corners,
        {
            "tt_27C/corner.cir": _deck(),
            "tt_27C/ngspice.log": "log",
            "tt_27C/netlist.expanded.cir": f".include {JOB_NETLIST}\nR1 a b 1k\n",
            "tt_27C/.spiceinit": (
                f"* from {JOB_NETLIST}\n* keep {JOB_NETLIST}.bak\nset numdgt=12\n"
            ),
        },
        client_netlist=str(tmp_path / "tb.spice"),
    )

    client = str(tmp_path / "tb.spice")
    corner_dir = local / "tt_27C"
    assert (corner_dir / "netlist.expanded.cir").read_text() == (
        f".include {client}\nR1 a b 1k\n"
    )
    # An exact path moves; a longer name that merely starts with it does not.
    assert (corner_dir / ".spiceinit").read_text() == (
        f"* from {client}\n* keep {JOB_NETLIST}.bak\nset numdgt=12\n"
    )


def test_rewrite_leaves_tokens_retained_includes_and_foreign_decks_alone(tmp_path):
    foreign = tmp_path / "other" / "corner.cir"
    foreign.parent.mkdir()
    foreign.write_text(_deck())  # another job's tree: not in `corners`
    decks = {
        "portable": _deck("$REQUEST_DIR/netlist.cir"),  # a token, not a job path
        "retained": _deck(f"{REMOTE}/tt_27C/netlist.expanded.cir"),  # kept copy
        "other_file": _deck("/var/tmp/eda-job-1/inputs/models.spice"),
    }
    corners = []
    for name, text in decks.items():
        path = tmp_path / f"{name}.cir"
        path.write_text(text)
        corners.append({"artifacts": {"deck": str(path)}})

    assert kept.rewrite_job_input_includes(corners, str(tmp_path / "tb.spice")) == 0

    for name, text in decks.items():
        assert (tmp_path / f"{name}.cir").read_text() == text
    assert foreign.read_text() == _deck()


def test_rewrite_tolerates_missing_decks_and_non_string_values(tmp_path):
    corners = [
        {"artifacts": None},
        {"artifacts": {"deck": None}},
        {"artifacts": {"deck": str(tmp_path / "gone" / "corner.cir")}},
        {},
    ]
    assert kept.rewrite_job_input_includes(corners, str(tmp_path / "tb.spice")) == 0


def test_install_without_a_client_netlist_keeps_the_deck_verbatim(tmp_path):
    corners = [_corner()]
    local = _install(
        tmp_path, corners, {"tt_27C/corner.cir": _deck(), "tt_27C/ngspice.log": "log"}
    )
    assert (local / "tt_27C" / "corner.cir").read_text() == _deck()


# --- the engine log ---------------------------------------------------------


def test_a_retained_log_is_installed_pointed_at_and_not_flagged(tmp_path):
    corners = [_corner()]
    local = _install(
        tmp_path,
        corners,
        {"tt_27C/corner.cir": _deck(), "tt_27C/ngspice.log": "solver output"},
        client_netlist=str(tmp_path / "tb.spice"),
        job_id="job-1",
    )
    log = Path(corners[0]["artifacts"]["log"])
    assert (
        log == local / "tt_27C" / "ngspice.log" and log.read_text() == "solver output"
    )
    assert corners[0]["diagnostics"] == []


def test_an_xyce_log_counts_as_retained(tmp_path):
    corner = _corner()
    corner["artifacts"]["log"] = f"{REMOTE}/tt_27C/xyce.log"
    _install(
        tmp_path,
        [corner],
        {"tt_27C/corner.cir": _deck(), "tt_27C/xyce.log": "xyce output"},
        job_id="job-1",
    )
    assert corner["diagnostics"] == []


def test_an_unreported_log_is_said_not_to_be_retained(tmp_path):
    corner = _corner(log=False)
    _install(tmp_path, [corner], {"tt_27C/corner.cir": _deck()}, job_id="job-9")

    (note,) = _noted(corner)
    assert note["severity"] == "info" and note["job_id"] == "job-9"
    assert note["message"].startswith("engine log not retained: batch job job-9")
    assert "artifacts.log is null" in note["message"]
    assert corner["artifacts"]["log"] is None  # existing fields unchanged
    assert corner["status"] == "pass"  # an `info` note never regrades


def test_a_reported_but_absent_log_is_flagged_too(tmp_path):
    corner = _corner()  # reports a log the collected tree does not hold
    _install(tmp_path, [corner], {"tt_27C/corner.cir": _deck()}, job_id="job-1")
    assert len(_noted(corner)) == 1


def test_a_tree_that_did_not_come_back_flags_every_corner(tmp_path):
    corners = [_corner("tt_27C"), _corner("ss_27C")]
    sb._install_batch_artifacts(
        corners,
        collected_dir=str(tmp_path / "nothing-collected"),
        artifacts_dir=str(tmp_path / "local"),
        client_netlist=str(tmp_path / "tb.spice"),
        job_id="job-1",
    )
    assert [len(_noted(c)) for c in corners] == [1, 1]
    assert not (tmp_path / "local").exists()


def test_a_unit_that_never_started_an_engine_is_not_flagged(tmp_path):
    corner = _corner(log=False, deck=False)
    corner["diagnostics"] = [
        {"severity": "error", "code": "budget_exceeded", "message": "no budget"}
    ]
    assert kept.note_unretained_logs([corner], job_id="job-1") == 0
    assert _noted(corner) == []


def test_a_corner_with_no_diagnostics_list_gets_one(tmp_path):
    corner = {"artifacts": {"log": None}, "diagnostics": None}
    assert kept.note_unretained_logs([corner]) == 1
    (note,) = corner["diagnostics"]
    assert note["message"].startswith("engine log not retained: the batch job returned")
    assert "job_id" not in note


def test_the_code_is_a_post_grading_marker_not_a_selectable_option():
    assert CODE == "engine_log_not_retained"
    assert CODE in sim._GRADING_MARKER_CODES
    assert CODE not in sim.DIAGNOSTIC_CODES


# --- end to end through one batch job ---------------------------------------


def _job_outputs(*, log: bool) -> dict[str, str]:
    report = json.loads(_sim_report())
    report["corners"][0]["artifacts"] = {
        "log": f"{REMOTE}/tt_1.800_27/ngspice.log" if log else None,
        "raw": None,
        "waveform": None,
        "deck": f"{REMOTE}/tt_1.800_27/corner.cir",
    }
    outputs = {
        "report.json": json.dumps(report),
        "artifacts/tt_1.800_27/corner.cir": _deck(),
    }
    if log:
        outputs["artifacts/tt_1.800_27/ngspice.log"] = "solver output"
    return outputs


def _run_kept_job(tmp_path, monkeypatch, *, log: bool):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = _job_outputs(log=log)
    artifacts = tmp_path / "artifacts"
    corners, _, environment = _run_failed_job(
        tmp_path,
        monkeypatch,
        runner,
        points=1,
        keep_artifacts=True,
        artifacts_dir=str(artifacts),
    )
    return corners[0], environment, artifacts


def test_a_kept_batch_job_returns_a_runnable_deck_and_its_log(tmp_path, monkeypatch):
    corner, _, artifacts = _run_kept_job(tmp_path, monkeypatch, log=True)

    assert Path(corner["artifacts"]["log"]).read_text() == "solver output"
    deck = Path(corner["artifacts"]["deck"])
    assert deck.parent == artifacts / "tt_1.800_27"
    submitted = tmp_path / "body.spice"  # `_run_failed_job`'s netlist_path
    assert f".include {submitted}\n" in deck.read_text()
    assert submitted.is_file() and JOB_NETLIST not in deck.read_text()
    assert corner["diagnostics"] == []


def test_a_kept_batch_job_without_a_log_names_the_job_in_the_note(
    tmp_path, monkeypatch
):
    corner, environment, _ = _run_kept_job(tmp_path, monkeypatch, log=False)

    (note,) = _noted(corner)
    assert environment["job_id"] == "job-1" and note["job_id"] == "job-1"
    assert corner["artifacts"]["log"] is None
    assert (tmp_path / "body.spice").as_posix() in Path(
        corner["artifacts"]["deck"]
    ).read_text()
