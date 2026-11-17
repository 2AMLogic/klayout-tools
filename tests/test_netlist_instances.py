"""The derived per-instance ``instances`` payload block (issue #5687).

``klt netlist`` writes the device multiplier on a ``+`` continuation line and
as both ``mult=N`` and ``m=N`` -- both properties are the PDK symbol's own
``format=`` attribute, so the written bytes cannot change. This module tests
the review surface instead: a structured ``instances`` array in the
``--format json`` payload (generate *and* ``--check``, derived from the
regenerated side), with each card's ``multiplier`` resolved tri-state.

Split from ``test_netlist.py``, which is at its file-size ceiling -- the same
reason ``test_netlist_erc.py`` and ``test_netlist_digest.py`` exist -- and
reusing the same stub-``xschem`` harness (a real subprocess, never a patched
``subprocess.run``). The batch-backend tests reuse ``test_netlist_batch``'s
hermetic fake-runner fixtures: the payload contract holds in both backends.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.netlist import run_netlist
from klayout_tools.netlist_instances import (
    INSTANCES_CAP,
    instances_payload,
    parse_instance_cards,
)
from test_netlist import (  # noqa: F401  (argv_log is a fixture)
    _NULL_RECORD,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
    argv_log,
)
from test_netlist_batch import (  # noqa: F401  (some are used by name only)
    RUNNER_TREE,
    _argv,
    _make_fake,
    _make_repo,
    _runner_outputs,
    _status,
)

pytestmark = pytest.mark.usefixtures("argv_log")

# --------------------------------------------------------------------------- #
# parsing: folding, extraction, the multiplier tri-state
# --------------------------------------------------------------------------- #

#: A wide sky130 FET card exactly as xschem writes it: the multiplier is on
#: the *second* continuation line, so `grep '^XM2'` shows neither form.
_WRAPPED_FET = """\
* header comment
.subckt block a b
XM2 n1 n2 n3 n4 sky130_fd_pr__nfet_01v8 L=1 W=2 nf=1 ad=0.58 as=0.58 pd=4.58
+ nrd=1 nrs=1 sa=0 sb=0 sd=0
+ mult=6 m=6
R1 a b 1k
.ends
.end
"""


def _fet_entry() -> dict:
    entries = parse_instance_cards(_WRAPPED_FET)
    assert [e["name"] for e in entries] == ["XM2", "R1"]
    return entries[0]


def test_wrapped_card_folds_continuations_into_params_and_model():
    entry = _fet_entry()
    assert entry["model"] == "sky130_fd_pr__nfet_01v8"
    # Every card segment is one folded card, continuation-line params included.
    assert entry["card"].startswith("XM2 n1 n2 n3 n4 sky130_fd_pr__nfet_01v8")
    assert entry["card"].endswith("mult=6 m=6")
    assert entry["card"].count("\n") == 0
    assert entry["params"]["nrd"] == "1"  # first continuation line
    assert entry["params"]["mult"] == "6"  # second continuation line
    assert entry["params"]["m"] == "6"
    assert entry["multiplier"] == {"m": "6", "mult": "6", "agree": True}


def test_multiplier_agree_is_false_when_the_forms_disagree():
    entry = parse_instance_cards("XM1 a b c d mod L=1 W=1\n+ mult=6 m=1\n")[0]
    assert entry["multiplier"] == {"m": "1", "mult": "6", "agree": False}


def test_multiplier_agree_is_null_when_fewer_than_two_forms_present():
    (solo_m,) = parse_instance_cards("X1 a b c d mod m=3\n")
    (solo_mult,) = parse_instance_cards("X1 a b c d mod mult=3\n")
    assert solo_m["multiplier"] == {"m": "3", "mult": None, "agree": None}
    assert solo_mult["multiplier"] == {"m": None, "mult": "3", "agree": None}


def test_multiplier_agree_is_null_never_a_crash_on_non_numeric_values():
    (expr,) = parse_instance_cards("X1 a b c d mod m={2*mult} mult=2\n")
    (word,) = parse_instance_cards("X1 a b c d mod m=wide mult=wide\n")
    assert expr["multiplier"]["agree"] is None
    assert word["multiplier"]["agree"] is None


def test_multiplier_agree_is_numeric_not_string_equality():
    # "6" vs "6.0" compares numerically. A SPICE suffix is not notation --
    # it is a scale factor -- so it never compares equal to an unsuffixed
    # value (see the suffix test below).
    (float_form,) = parse_instance_cards("X1 a b c d mod mult=6.0 m=6\n")
    assert float_form["multiplier"]["agree"] is True


def test_multiplier_suffixed_values_are_not_applicable_never_equal():
    """A SPICE suffix is a scale factor (``6u`` is 6e-6, ``6m`` is 6e-3), so
    stripping it to compare mantissas would report agreement across orders
    of magnitude -- the unit-suffixed-literal failure ``pdk_models.py``
    documents from the ``.option scale=1.0u`` incident (#1396). A suffixed
    value therefore takes the same "not numeric" path as an ``expr(...)``:
    ``agree`` is ``null``, never a scale-blind ``true``."""
    (mismatch,) = parse_instance_cards("X1 a b c d mod mult=6 m=6m\n")
    assert mismatch["multiplier"] == {"m": "6m", "mult": "6", "agree": None}
    (same_suffix,) = parse_instance_cards("X1 a b c d mod mult=6u m=6u\n")
    assert same_suffix["multiplier"] == {"m": "6u", "mult": "6u", "agree": None}


def test_comments_and_control_cards_are_not_instances():
    entries = parse_instance_cards(_WRAPPED_FET)
    assert [e["name"] for e in entries] == ["XM2", "R1"]
    # A leading '+' with nothing to fold onto stays unfolded -- and unparsed.
    orphans = parse_instance_cards("+ mult=6 m=6\nX1 a b c d mod m=1\n")
    assert [e["name"] for e in orphans] == ["X1"]


def test_a_bare_positional_card_is_still_an_instance_with_null_model():
    (r1,) = parse_instance_cards("R1 a b 1k\n")
    assert r1["model"] is None
    assert r1["params"] == {}
    assert r1["multiplier"] == {"m": None, "mult": None, "agree": None}
    (src,) = parse_instance_cards("V1 a 0 dc 1.8\n")
    assert src["model"] is None


def test_a_duplicate_parameter_keys_last_one_wins():
    (x1,) = parse_instance_cards("X1 a b c d mod m=1 m=2\n")
    assert x1["params"]["m"] == "2"
    assert x1["multiplier"] == {"m": "2", "mult": None, "agree": None}


def test_zero_device_instances_is_an_empty_array_not_a_missing_key():
    assert instances_payload(b"* only comments\n.end\n") == {
        "instances": [],
        "instances_truncated": False,
    }


def test_instances_cap_truncates_and_flags():
    deck = "".join(f"X{i} a b c d mod m=1\n" for i in range(5))
    block = instances_payload(deck.encode(), cap=2)
    assert len(block["instances"]) == 2
    assert block["instances_truncated"] is True
    # Default cap: the documented constant, generous for any real block.
    assert instances_payload(deck.encode())["instances_truncated"] is False
    assert INSTANCES_CAP >= 1


# --------------------------------------------------------------------------- #
# payload wiring (stub xschem, both modes)
# --------------------------------------------------------------------------- #

_WIDE_BODY = (
    "* stub netlist\n.subckt block a b\n"
    "XM2 a b 0 0 sky130_fd_pr__nfet_01v8 L=1 W=2\n+ mult=6 m=6\n"
    ".ends\n.end"
)


def test_generate_payload_carries_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_BODY", _WIDE_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    payload = run_netlist(
        schematic, str(tmp_path / "block.spice"), xschem_binary=binary
    )

    assert payload["mode"] == "generate"
    (xm2,) = payload["instances"]
    assert xm2["name"] == "XM2"
    assert xm2["params"]["mult"] == "6"
    assert xm2["multiplier"] == {"m": "6", "mult": "6", "agree": True}
    assert payload["instances_truncated"] is False


def test_generate_payload_instances_is_empty_not_absent_without_devices(
    tmp_path,
):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    payload = run_netlist(
        schematic, str(tmp_path / "block.spice"), xschem_binary=binary
    )

    assert payload["instances"] == []
    assert payload["instances_truncated"] is False


def test_check_payload_instances_come_from_the_regenerated_side(tmp_path, monkeypatch):
    """``--check`` compares committed vs regenerated; ``instances`` must
    describe the regenerated side (what the schematics produce now), even
    when the committed file's own multipliers differ."""
    monkeypatch.setenv("NETLIST_BODY", _WIDE_BODY)  # regenerated: mult=6
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    committed = _WIDE_BODY.replace("mult=6 m=6", "mult=1 m=1") + "\n"
    output.write_text(committed, encoding="utf-8")

    payload = run_netlist(schematic, str(output), check=True, xschem_binary=binary)

    assert payload["status"] == "drifted"
    (xm2,) = payload["instances"]
    assert xm2["params"]["mult"] == "6"  # the regenerated side, not the "1"
    assert xm2["multiplier"]["agree"] is True


def test_written_netlist_bytes_are_unchanged_by_the_instances_view(
    tmp_path, monkeypatch
):
    """The mechanical half of the "written netlist unchanged" criterion: the
    file on disk is exactly the inputs record plus the netlist body -- the
    instances view is derived, never written."""
    monkeypatch.setenv("NETLIST_BODY", _WIDE_BODY)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    run_netlist(schematic, str(output), xschem_binary=binary)

    assert output.read_bytes() == (_NULL_RECORD + _WIDE_BODY + "\n").encode()

    output.unlink()
    run_netlist(
        schematic,
        str(output),
        xschem_binary=binary,
        provenance_header=False,
    )
    assert output.read_bytes() == (_WIDE_BODY + "\n").encode()


# --------------------------------------------------------------------------- #
# batch backend: the payload contract holds in both backends (PR #6096)
# --------------------------------------------------------------------------- #

#: The batch runner's collected netlist: the same wide device as
#: ``_WIDE_BODY``, shaped like ``test_netlist_batch``'s ``_BODY`` (the
#: ``sch_path`` comment is what the client rewrites from the runner's scratch
#: tree to this checkout).
_BATCH_WIDE = (
    "** sch_path: {sch}\n"
    "**.subckt block a b\n"
    "XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1 nf=1\n"
    "+ mult=6 m=6\n"
    "**.ends\n"
    ".end\n"
)


def test_batch_payload_carries_instances(tmp_path, monkeypatch, capsys):
    """``instances`` is documented as present in every ``--format json``
    payload; the batch backend derives it from the collected (regenerated)
    netlist bytes exactly as the local backend does."""
    repo = _make_repo(tmp_path)
    fake = _make_fake(tmp_path, monkeypatch)
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(
        _BATCH_WIDE.format(sch=f"{RUNNER_TREE}/design/block.sch")
    )
    output = tmp_path / "out" / "block.spice"

    assert main(_argv(repo, output, "--backend", "batch")) == 0
    payload = json.loads(capsys.readouterr().out)

    (xm1,) = payload["instances"]
    assert xm1["name"] == "XM1"
    assert xm1["params"]["mult"] == "6"  # folded off the continuation line
    assert xm1["multiplier"] == {"m": "6", "mult": "6", "agree": True}
    assert payload["instances_truncated"] is False


def test_batch_check_payload_instances_come_from_the_regenerated_side(
    tmp_path, monkeypatch, capsys
):
    """Under ``--backend batch --check``, ``instances`` describes the side the
    runner produced, not the committed file -- the local backend's rule."""
    repo = _make_repo(tmp_path)
    fake = _make_fake(tmp_path, monkeypatch)
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(
        _BATCH_WIDE.format(sch=f"{RUNNER_TREE}/design/block.sch")
    )
    committed = tmp_path / "block.spice"
    committed.write_text(
        _BATCH_WIDE.format(sch="/home/elsewhere/design/block.sch").replace(
            "mult=6 m=6", "mult=1 m=1"
        ),
        encoding="utf-8",
    )

    assert main(_argv(repo, committed, "--backend", "batch", "--check")) == 3
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "drifted"
    (xm1,) = payload["instances"]
    assert xm1["params"]["mult"] == "6"  # the regenerated side, not the "1"
    assert xm1["multiplier"]["agree"] is True


def test_batch_payload_instances_is_empty_not_absent_without_devices(
    tmp_path, monkeypatch, capsys
):
    """The "``[]``, never an absent key" guarantee holds on the batch backend
    too: a consumer may index ``payload["instances"]`` unconditionally."""
    repo = _make_repo(tmp_path)
    fake = _make_fake(tmp_path, monkeypatch)
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs("* only comments\n.end\n")
    output = tmp_path / "out" / "block.spice"

    assert main(_argv(repo, output, "--backend", "batch")) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["instances"] == []
    assert payload["instances_truncated"] is False
