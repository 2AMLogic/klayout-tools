"""``klt signoff --attest 9``: a ``cold_start`` list -- several named cold
starts over one inventoried file (issue #5641).

N testbenches sharing one implementation module previously had exactly one way
to get N cold starts attested: mint one wrapper file per testbench so each had
a unique path. The wrappers then carry the attested ``content_hash`` while the
module implementing every graded behaviour is attested by nothing. The list
form lets one entry declare several cold starts over the same file: the file is
hashed once, each member is graded independently under its own ``name``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import AttestationError, build_attestation

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_BAD = [sys.executable, "-c", "import sys; sys.exit(1)"]
_PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}
_TWO = [{"name": "ac", "argv": _OK}, {"name": "tran", "argv": _BAD}]


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb").mkdir()
    (repo / "attest").mkdir()
    (repo / "tb" / "shared.py").write_text("# the one shared implementation\n")
    (repo / "tb" / "a.spice").write_text("* a\n")
    (repo / "tb" / "b.spice").write_text("* b\n")
    return repo


def _inv(repo: Path, **fields) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 9,
        "pins": _PINS,
        **fields,
    }
    path = repo / "attest" / "item9.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _checks(envelope: dict) -> list[dict]:
    return envelope["attestation"]["checks"]


def _commands(envelope: dict) -> list[dict]:
    return [c for c in _checks(envelope) if c["check"] == "command"]


def test_list_form_hashes_the_file_once_and_grades_each_member(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9, _inv(repo, testbenches=[{"path": "tb/shared.py", "cold_start": _TWO}])
    )

    present = [c for c in _checks(envelope) if c["check"] == "present"]
    assert [c["path"] for c in present] == ["tb/shared.py"]
    assert [c["role"] for c in present] == ["testbench"]

    commands = _commands(envelope)
    assert [c["name"] for c in commands] == ["ac", "tran"]
    assert {c["testbench"] for c in commands} == {"tb/shared.py"}
    by_name = {c["name"]: c for c in commands}
    assert by_name["ac"] == {
        "check": "command",
        "role": "cold_start",
        "testbench": "tb/shared.py",
        "name": "ac",
        "argv": _OK,
        "cwd": ".",
        "exit_status": 0,
        "passed": True,
    }
    assert by_name["tran"]["exit_status"] == 1
    assert by_name["tran"]["passed"] is False
    assert envelope["status"] == "fail"  # the failing member gates the envelope


def test_single_object_form_emits_no_name(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[{"path": "tb/shared.py", "cold_start": {"argv": _OK}}],
        ),
    )

    assert _commands(envelope) == [
        {
            "check": "command",
            "role": "cold_start",
            "testbench": "tb/shared.py",
            "argv": _OK,
            "cwd": ".",
            "exit_status": 0,
            "passed": True,
        }
    ]


def test_members_are_emitted_in_name_order_not_declaration_order(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {
                    "path": "tb/shared.py",
                    "cold_start": [
                        {"name": "tran", "argv": _OK},
                        {"name": "ac", "argv": _OK},
                    ],
                }
            ],
        ),
    )

    assert [c["name"] for c in _commands(envelope)] == ["ac", "tran"]


@pytest.mark.parametrize(
    "cold_start, message",
    [
        ([], "cold_start' is an empty list"),
        ([_TWO[0], "nope"], "must be an object with a 'name'"),
        ([{"argv": _OK}], r"cold_start\[0\]\.name"),
        ([{"name": "", "argv": _OK}], "must be a non-empty string"),
        ([{"name": 7, "argv": _OK}], "must be a non-empty string"),
        ([{"name": "ac", "argv": _OK}, {"name": "ac", "argv": _OK}], "more than once"),
    ],
)
def test_malformed_cold_start_lists_are_refused(tmp_path, cold_start, message):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=message):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[{"path": "tb/shared.py", "cold_start": cold_start}],
            ),
        )


def test_duplicate_object_form_paths_name_the_list_form(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="listed more than once.*cold_start"):
        build_attestation(
            9,
            _inv(
                repo,
                testbenches=[
                    {"path": "tb/shared.py", "cold_start": {"argv": _OK}},
                    {"path": "tb/shared.py", "cold_start": {"argv": _OK}},
                ],
            ),
        )


def test_mixed_forms_with_top_level_cold_start_all_run(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                "tb/a.spice",
                {"path": "tb/b.spice", "cold_start": {"argv": _OK}},
                {"path": "tb/shared.py", "cold_start": _TWO},
            ],
            cold_start={"argv": _OK},
        ),
    )

    assert envelope["status"] == "fail"  # tran fails
    commands = _commands(envelope)
    assert [(c.get("testbench"), c.get("name")) for c in commands] == [
        (None, None),  # top-level cold start
        ("tb/b.spice", None),  # single-object form
        ("tb/shared.py", "ac"),
        ("tb/shared.py", "tran"),
    ]
    present = [c for c in _checks(envelope) if c["check"] == "present"]
    assert [c["path"] for c in present] == ["tb/a.spice", "tb/b.spice", "tb/shared.py"]


def test_one_member_list_is_legal_and_emits_a_name(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(
        9,
        _inv(
            repo,
            testbenches=[
                {"path": "tb/shared.py", "cold_start": [{"name": "smoke", "argv": _OK}]}
            ],
        ),
    )

    assert envelope["status"] == "pass"
    assert _commands(envelope) == [
        {
            "check": "command",
            "role": "cold_start",
            "testbench": "tb/shared.py",
            "name": "smoke",
            "argv": _OK,
            "cwd": ".",
            "exit_status": 0,
            "passed": True,
        }
    ]


def test_text_output_labels_a_named_cold_start_by_its_name(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _inv(repo, testbenches=[{"path": "tb/shared.py", "cold_start": _TWO}])

    assert main(["signoff", "--attest", "9", "--inventory", inventory]) == 3

    out = capsys.readouterr().out
    assert "cold_start ac: " in out
    assert "cold_start tran: " in out
    # The unnamed single-object form still labels by its testbench path.
    assert "cold_start tb/shared.py" not in out


def test_named_member_progress_lines_carry_the_name(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _inv(repo, testbenches=[{"path": "tb/shared.py", "cold_start": _TWO}])

    assert main(["signoff", "--attest", "9", "--inventory", inventory]) == 3

    err = capsys.readouterr().err
    assert "running cold_start ac tb/shared.py ..." in err
    assert "running cold_start tran tb/shared.py ..." in err
