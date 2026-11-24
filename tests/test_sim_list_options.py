"""`klt sim` list requests validate a supplied top-level ``options`` (#7720).

Deck-list (`sim_decks`) and command-list (`sim_commands`) requests must refuse
a non-object ``options`` -- a truthy scalar (once an uncaught
``AttributeError`` on the deck path) and falsey ``false``/``[]``/``0`` (once
silently run as the defaults) -- with the same field-specific ``SimError`` a
corner request gets, before any unit runs or job is submitted. Omitted, null
and object values keep their behavior. Kept out of ``test_sim_decks.py`` /
``test_sim_commands.py`` (file-size ratchet) but built on their fixtures.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import test_sim_commands as tc
import test_sim_decks as td
from klayout_tools import sim, sim_batch
from klayout_tools.cli import main
from klayout_tools.sim import SimError

BAD_OPTIONS = ["typo", False, [], 0, 1.5]
_BAD_IDS = ["str", "false", "list", "zero", "float"]
_MESSAGE = r"request\.options must be an object \(got (str|bool|list|int|float)\)"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(sim.sim_commands.APPROVALS_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


@pytest.fixture
def no_execution(monkeypatch):
    """Fail the test if anything spawns a process or reaches the batch fleet."""
    calls: list[str] = []

    def record(name):
        def _called(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"invalid request reached {name}")

        return _called

    monkeypatch.setattr(subprocess, "run", record("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", record("subprocess.Popen"))
    for name in ("submit_job", "launch_job", "poll_status", "collect_outputs"):
        monkeypatch.setattr(sim_batch, name, record(f"sim_batch.{name}"))
    return calls


def _deck_path(tmp_path, **overrides):
    return str(td.write_request(tmp_path, td.deck_request(tmp_path, **overrides)))


def _command_path(tmp_path, **fields):
    unit = {"id": "u1", "argv": ["sh", "-c", tc.SCRIPT], "outputs": ["out.txt"]}
    return tc._write(tmp_path / "req.json", {"commands": [unit], **fields})


# --------------------------------------------------------------------------- #
# Malformed options are refused before anything runs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("options", BAD_OPTIONS, ids=_BAD_IDS)
def test_deck_list_dry_run_rejects_non_object_options(tmp_path, options, no_execution):
    path = _deck_path(tmp_path, options=options)
    with pytest.raises(SimError, match=_MESSAGE):
        sim.run_sim(path, backend="local", dry_run=True)
    assert no_execution == []


@pytest.mark.parametrize("options", BAD_OPTIONS, ids=_BAD_IDS)
def test_deck_list_batch_rejects_non_object_options(tmp_path, options, no_execution):
    path = str(
        td.write_request(tmp_path, td.batch_deck_request(tmp_path, options=options))
    )
    with pytest.raises(SimError, match=_MESSAGE):
        sim.run_sim(path, backend="batch")
    assert no_execution == []


@pytest.mark.parametrize("options", BAD_OPTIONS, ids=_BAD_IDS)
def test_command_list_local_rejects_non_object_options(tmp_path, options, no_execution):
    path = _command_path(tmp_path, options=options)
    with pytest.raises(SimError, match=_MESSAGE):
        sim.run_sim(
            path,
            backend="local",
            command_approvals=tc._approvals(tmp_path),
            artifacts_dir=str(tmp_path / "out"),
        )
    assert no_execution == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("options", BAD_OPTIONS, ids=_BAD_IDS)
def test_planners_reject_non_object_options(tmp_path, options):
    deck = td.deck_request(tmp_path, options=options)
    with pytest.raises(SimError, match=_MESSAGE):
        sim.sim_decks.plan_deck_units(deck, request_dir=str(tmp_path))
    command = {"commands": [{"id": "u1", "argv": ["true"]}], "options": options}
    with pytest.raises(SimError, match=_MESSAGE):
        sim.sim_commands.plan_command_units(
            command, request_dir=str(tmp_path), approvals=[]
        )


def _assert_one_json_error(capsys, rc):
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    envelope = json.loads(captured.err)  # exactly one JSON document
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "sim"
    assert envelope["error"]["message"] == "request.options must be an object (got str)"


def test_cli_json_deck_dry_run_envelope(tmp_path, capsys, no_execution):
    path = _deck_path(tmp_path, options="typo")
    argv = ["sim", path, "--backend", "local", "--dry-run", "--format", "json"]
    _assert_one_json_error(capsys, main(argv))
    assert no_execution == []


def test_cli_json_command_list_envelope(tmp_path, capsys, no_execution):
    path = _command_path(tmp_path, options="typo")
    approvals = tc._approvals(tmp_path)
    argv = ["sim", path, "--backend", "local", "--format", "json"]
    _assert_one_json_error(capsys, main([*argv, "--command-approvals", approvals]))
    assert no_execution == []


# --------------------------------------------------------------------------- #
# Controls: omission, null and objects keep their behavior; other refusals stay
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "options", ["omit", None, {}], ids=["omitted", "null", "empty"]
)
def test_deck_list_dry_run_accepts_absent_or_object_options(tmp_path, options):
    request = td.deck_request(tmp_path)
    if options == "omit":
        del request["options"]
    else:
        request["options"] = options
    path = str(td.write_request(tmp_path, request))
    report = sim.run_sim(path, backend="local", dry_run=True)
    assert report["status"] == "valid"


@pytest.mark.parametrize(
    "fields",
    [{}, {"options": None}, {"options": {}}, {"options": {"timeout_s": 30}}],
    ids=["omitted", "null", "empty", "object"],
)
def test_command_list_runs_with_absent_or_object_options(tmp_path, fields):
    path = _command_path(tmp_path, **fields)
    approvals = tc._approvals(tmp_path)
    report = sim.run_sim(path, backend="local", command_approvals=approvals)
    assert report["status"] == "pass"


def test_command_list_still_requires_approval(tmp_path):
    with pytest.raises(SimError, match="approv"):
        sim.run_sim(_command_path(tmp_path, options={}), backend="local")


def test_command_list_dry_run_still_refused(tmp_path):
    with pytest.raises(SimError, match="--dry-run"):
        sim.run_sim(
            _command_path(tmp_path, options={}),
            backend="local",
            dry_run=True,
            command_approvals=tc._approvals(tmp_path),
        )


def test_monte_carlo_still_corner_only_in_both_lists(tmp_path):
    deck = _deck_path(tmp_path, monte_carlo={"samples": 2})
    with pytest.raises(SimError, match="monte_carlo"):
        sim.run_sim(deck, backend="local", dry_run=True)
    command = _command_path(tmp_path, options={}, monte_carlo={"samples": 2})
    with pytest.raises(SimError, match="monte_carlo"):
        sim.run_sim(command, backend="local", command_approvals=tc._approvals(tmp_path))
