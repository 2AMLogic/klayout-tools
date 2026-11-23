"""Remote corner collection and raw-deck units retain model-selection evidence."""

import json

import pytest

from klayout_tools import sim
from klayout_tools.sim_spiceinit import MODEL_SELECTION_INIT_HINT
from test_sim import (
    _base_remote_request,
    _install_fake_remote_transport,
    _write_body,
    _write_request,
)
from test_sim_batch import _sim_report
from test_sim_decks import (
    deck_request,
    install_fake_remote,
    remote_deck_request,
    run_local,
)

MODEL_ERROR = "Error on line 4: could not find a valid modelname"


@pytest.mark.parametrize(
    ("message", "lines", "expected"),
    [
        (MODEL_ERROR, [], True),
        (MODEL_ERROR, ["set ngbehavior=hsa"], False),
        ("Error on line 4: unknown parameter", [], False),
    ],
)
def test_remote_corner_preserves_abort_and_conditionally_hints(
    tmp_path, monkeypatch, message, lines, expected
):
    worker = json.loads(_sim_report(("default/novdd/27C",)))
    worker.update(status="error", errored=1, passed=0)
    fatal = {"code": "ngspice_fatal", "severity": "error", "message": message}
    worker["corners"][0].update(status="error", diagnostics=[fatal])
    _install_fake_remote_transport(
        monkeypatch, remote_report_factory=lambda state: worker
    )
    _write_body(tmp_path)
    request = _write_request(
        tmp_path, _base_remote_request(options={"ngspice_init": lines})
    )
    result = sim.run_sim(str(request))
    assert result["status"] == "error"
    assert fatal in result["corners"][0]["diagnostics"]
    codes = [d["code"] for d in result.get("diagnostics", [])]
    assert codes.count(MODEL_SELECTION_INIT_HINT) == int(expected)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize(
    ("message", "lines", "expected"),
    [
        (MODEL_ERROR, [], True),
        (MODEL_ERROR, ["set ngbehavior=hsa"], False),
        ("Error on line 4: unknown parameter", [], False),
    ],
)
def test_raw_deck_hint_is_scoped_to_the_failing_unit(
    tmp_path, monkeypatch, remote, message, lines, expected
):
    decks = [{"id": "bad", "deck": "bad.spice"}, {"id": "good", "deck": "deck.spice"}]
    if remote:
        install_fake_remote(monkeypatch, tmp_path)
        request = remote_deck_request(tmp_path, decks=decks)
    else:
        request = deck_request(tmp_path, decks=decks)
    request["options"]["ngspice_init"] = lines
    (tmp_path / "bad.spice").write_text(
        f"* stub-exit: 1\n* stub-emit-stderr-text: {message}\n"
    )
    result = run_local(tmp_path, request)
    bad, good = result["units"]
    assert result["status"] == bad["status"] == "error"
    assert bad["exit_code"] == 1
    assert good["status"] == "pass"
    codes = [d["code"] for d in bad["diagnostics"]]
    assert "ngspice_fatal" in codes
    assert codes.count(MODEL_SELECTION_INIT_HINT) == int(expected)
    assert MODEL_SELECTION_INIT_HINT not in [d["code"] for d in good["diagnostics"]]
    assert "diagnostics" not in result
