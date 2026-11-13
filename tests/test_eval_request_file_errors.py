"""Tests that `klt eval`'s two request-file arguments are distinguishable
when one of them is unreadable or malformed (issue #5530).

A sibling of `tests/test_eval.py` rather than an addition to it: that file
sits at its per-file size ratchet ceiling (`file-size-baseline.json`), and
raising the ceiling was explicitly out of scope for #5530 -- the same split
move issue #5426 made for `gen_compose_routing.py`.

`klt eval <descriptor> --candidate <candidate>` routes **both** paths
through `klayout_tools._paths._load_request_json`, so the loader's own
read-failure and parse-failure messages are the only thing telling the
caller which of the two files is at fault.
"""

from __future__ import annotations

import json
import re

import pytest

from klayout_tools.eval import EvalError, run_eval

_MINIMAL_DESCRIPTOR = {
    "gates": [{"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}}],
    "objective": {
        "check": "layout-metrics",
        "metric": "cell_count",
        "polarity": "minimize",
        "args": {"block": "${block}"},
    },
}


def _write_candidate(tmp_path, name: str = "candidate.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"block": "block", "layout": "block/layout.gds"}))
    return str(path)


def _write_descriptor(tmp_path, name: str = "descriptor.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(_MINIMAL_DESCRIPTOR))
    return str(path)


def test_malformed_descriptor_names_the_descriptor_not_the_candidate(tmp_path):
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text("{not json")
    candidate_path = _write_candidate(tmp_path)
    with pytest.raises(EvalError, match="is not valid JSON") as excinfo:
        run_eval(str(descriptor_path), candidate_path)
    message = str(excinfo.value)
    assert str(descriptor_path) in message
    assert candidate_path not in message


def test_malformed_candidate_names_the_candidate_not_the_descriptor(tmp_path):
    descriptor_path = _write_descriptor(tmp_path)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text("{not json")
    with pytest.raises(EvalError, match="is not valid JSON") as excinfo:
        run_eval(descriptor_path, str(candidate_path))
    message = str(excinfo.value)
    assert str(candidate_path) in message
    assert descriptor_path not in message


def test_undecodable_descriptor_names_the_descriptor(tmp_path):
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_bytes(b"\xff\xfe{}")
    candidate_path = _write_candidate(tmp_path)
    with pytest.raises(EvalError, match=re.escape(str(descriptor_path))) as excinfo:
        run_eval(str(descriptor_path), candidate_path)
    message = str(excinfo.value)
    assert "could not read request" in message
    assert candidate_path not in message


def test_undecodable_candidate_names_the_candidate(tmp_path):
    descriptor_path = _write_descriptor(tmp_path)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(EvalError, match=re.escape(str(candidate_path))) as excinfo:
        run_eval(descriptor_path, str(candidate_path))
    message = str(excinfo.value)
    assert "could not read request" in message
    assert descriptor_path not in message
