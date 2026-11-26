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

import pytest

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


def test_rewrite_leaves_tokens_missing_expansions_and_foreign_decks_alone(tmp_path):
    foreign = tmp_path / "other" / "corner.cir"
    foreign.parent.mkdir()
    foreign.write_text(_deck())  # another job's tree: not in `corners`
    decks = {
        "portable": _deck("$REQUEST_DIR/netlist.cir"),  # a token, not a job path
        "retained": _deck(f"{REMOTE}/tt_27C/netlist.expanded.cir"),  # kept copy
        "other_file": _deck("/var/tmp/eda-job-1/inputs/models.spice"),
    }
    files = {f"{name}/corner.cir": text for name, text in decks.items()}
    local = _install(
        tmp_path,
        [_corner(name) for name in decks],
        files,
        client_netlist=str(tmp_path / "tb.spice"),
    )

    for name, text in files.items():
        assert (local / name).read_text() == text
    assert foreign.read_text() == _deck()


def test_kept_input_with_spaces_is_one_spice_include_argument(tmp_path):
    client = tmp_path / "a bench" / "divider.spice"
    for old in (JOB_NETLIST, f'"{JOB_NETLIST}"', f"'{JOB_NETLIST}'"):
        local = _install(
            tmp_path,
            [_corner()],
            {"tt_27C/corner.cir": _deck(old)},
            client_netlist=str(client),
        )
        quote = "'" if old.startswith("'") else '"'
        assert (local / "tt_27C/corner.cir").read_text() == _deck(
            f"{quote}{client}{quote}"
        )


def test_an_expanded_worker_deck_includes_its_installed_sibling(tmp_path):
    remote = f"{REMOTE}/tt_27C/netlist.expanded.cir"
    files = {
        "tt_27C/corner.cir": _deck(remote),
        "tt_27C/netlist.expanded.cir": "R1 a b 1k\n",
    }
    local = _install(
        tmp_path, [_corner()], files, client_netlist=str(tmp_path / "original.cir")
    )
    expanded = local / "tt_27C/netlist.expanded.cir"
    assert (local / "tt_27C/corner.cir").read_text() == _deck(str(expanded))
    assert expanded.read_text() == files["tt_27C/netlist.expanded.cir"]


def test_rewrite_tolerates_missing_decks_and_non_string_values(tmp_path):
    corners = [
        {"artifacts": None},
        {"artifacts": {"deck": None}},
        {"artifacts": {"deck": str(tmp_path / "gone" / "corner.cir")}},
        {},
    ]
    local = _install(
        tmp_path,
        [_corner(), *corners],
        {"tt_27C/corner.cir": _deck()},
        client_netlist=str(tmp_path / "tb.spice"),
    )
    assert (local / "tt_27C/corner.cir").read_text() == _deck(
        str(tmp_path / "tb.spice")
    )


def test_install_only_rewrites_decks_and_siblings_from_this_collection(tmp_path):
    local = tmp_path / "local"
    outside = tmp_path / "another-run"
    stale = local / "previous"
    current = local / "current"
    for directory in (outside, stale, current):
        directory.mkdir(parents=True)
    original = _deck().encode() + b"* original bytes\r\n"
    protected = [
        outside / "corner.cir",
        outside / ".spiceinit",
        outside / "netlist.expanded.cir",
        stale / "corner.cir",
        current / ".spiceinit",
    ]
    for path in protected:
        path.write_bytes(original)

    corners = [_corner("current"), _corner("previous"), _corner("../another-run")]
    client = tmp_path / "tb.spice"
    _install(
        tmp_path, corners, {"current/corner.cir": _deck()}, client_netlist=str(client)
    )

    assert (current / "corner.cir").read_text() == _deck(str(client))
    for path in protected:
        assert path.read_bytes() == original


@pytest.mark.parametrize("link", ["directory symlink", "file symlink", "hard link"])
def test_install_protects_linked_destinations(tmp_path, link):
    outside = tmp_path / "original"
    outside.mkdir()
    original = _deck().encode()
    (outside / ".spiceinit").write_bytes(original)
    local = tmp_path / "local"
    local.mkdir()
    corner = local / "tt_27C"
    if link == "directory symlink":
        corner.symlink_to(outside, target_is_directory=True)
    else:
        corner.mkdir()
        if link == "file symlink":
            (corner / ".spiceinit").symlink_to(outside / ".spiceinit")
        else:
            (corner / ".spiceinit").hardlink_to(outside / ".spiceinit")

    with pytest.raises(sb.BatchError):
        _install(
            tmp_path,
            [_corner()],
            {"tt_27C/.spiceinit": "collected"},
            client_netlist="tb.spice",
        )
    assert (outside / ".spiceinit").read_bytes() == original


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


@pytest.mark.parametrize("log_name", ["ngspice.log", "xyce.log"])
def test_a_previous_jobs_log_does_not_count_as_retained(tmp_path, log_name):
    local = tmp_path / "local"
    relative_log = f"tt_27C/{log_name}"

    def install(job_id, files):
        corner = _corner()
        corner["artifacts"]["log"] = f"{REMOTE}/{relative_log}"
        sb._install_batch_artifacts(
            [corner],
            collected_dir=str(_collected(tmp_path / job_id, files)),
            artifacts_dir=str(local),
            job_id=job_id,
        )
        return corner

    first = install("job-1", {relative_log: "job 1 output"})
    assert first["diagnostics"] == []
    second = install("job-2", {"tt_27C/corner.cir": _deck()})
    assert (
        second["artifacts"]["log"]
        == first["artifacts"]["log"]
        == str(local / relative_log)
    )
    assert (local / relative_log).read_text() == "job 1 output"
    (note,) = _noted(second)
    assert note["job_id"] == "job-2"


@pytest.mark.parametrize("reported", [False, True], ids=["null-log", "reported-path"])
def test_a_missing_log_is_said_not_to_be_retained(tmp_path, reported):
    corner = _corner(log=reported)
    local = _install(tmp_path, [corner], {"tt_27C/corner.cir": _deck()}, job_id="job-9")

    (note,) = _noted(corner)
    assert note["severity"] == "info" and note["job_id"] == "job-9"
    assert note["message"] == (
        "engine log not retained: batch job job-9 returned no matching engine log "
        "for this corner. Inspect the job's outputs/ prefix or re-run the corner "
        "with --backend local."
    )
    assert corner["artifacts"]["log"] == (
        str(local / "tt_27C/ngspice.log") if reported else None
    )
    assert corner["status"] == "pass"  # an `info` note never regrades


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
    assert (
        kept.note_unretained_logs([corner], {}, remote_root=None, job_id="job-1") == 0
    )
    assert _noted(corner) == []


def test_a_corner_with_no_diagnostics_list_gets_one(tmp_path):
    corner = {"artifacts": {"log": None}, "diagnostics": None}
    assert kept.note_unretained_logs([corner], {}, remote_root=None) == 1
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
