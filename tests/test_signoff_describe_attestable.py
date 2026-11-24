"""The grader describes the attestation producers the CLI actually runs."""

import argparse
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.cli.parser import create_parser
from klayout_tools.signoff import describe_grader
from klayout_tools.signoff_attest import ATTESTABLE_ITEMS, STREAM_ATTESTABLE_ITEM
from test_signoff_attest_description import _inventory, _repo, _stream


def test_describe_grader_attestable_choices():
    actions = create_parser()._actions
    verbs = next(
        action for action in actions if isinstance(action, argparse._SubParsersAction)
    )
    attest = next(
        action
        for action in verbs.choices["signoff"]._actions
        if action.dest == "attest"
    )
    advertised = {int(item) for item in describe_grader()["attestable_items"]}
    assert (
        set(attest.choices) == advertised == {*ATTESTABLE_ITEMS, STREAM_ATTESTABLE_ITEM}
    )


@pytest.mark.parametrize("item", sorted((*ATTESTABLE_ITEMS, STREAM_ATTESTABLE_ITEM)))
def test_describe_grader_attestable_input_mints_advertised_kind(tmp_path, capsys, item):
    repo = _repo(tmp_path)
    capability = describe_grader()["attestable_items"][str(item)]
    if item == STREAM_ATTESTABLE_ITEM:
        assert capability["input_flag"] == "--input"
        source = _stream(repo)
    else:
        assert capability["input_flag"] == "--inventory"
        if item == 8:
            (repo / "evidence/sim_tt.json").write_text(
                json.dumps({"schema_version": 1, "kind": "generic", "status": "pass"})
            )
        source = repo / "inventory.json"
        source.write_text(json.dumps(_inventory(item)))
    assert (
        main(
            [
                "signoff",
                "--attest",
                str(item),
                capability["input_flag"],
                str(source),
                "--format",
                "json",
            ]
        )
        == 0
    )
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["t1_item"] == item
    assert envelope["kind"] == capability["envelope_kind"] == "generic"
