"""`klt sim --op-lint` shape-checks a supplied top-level `measurements`
(issue #6590).

The default `klt sim` mode refuses a malformed `measurements` container or
entry through the shared error envelope (issue #6245); `--op-lint` reads the
same request document but used to skip that guard. A truthy non-list scalar
escaped as a `TypeError` traceback, and `null` / a string / a non-object
entry were silently dropped, so one document was accepted here and refused
there. `op_sanity.load_request` now calls the shared
`sim_case_names.validate_measurement_shapes` and raises `OpSanityError`, which
`klt sim --op-lint` already turns into the envelope. Split out of
test_op_sanity.py for the file-size ratchet."""

from __future__ import annotations

import json

import pytest

from klayout_tools import op_sanity
from klayout_tools.cli import main
from test_op_sanity import (
    _GOOD_NETLIST,
    _SATURATED_NMOS,
    _SATURATED_PMOS,
    _findings,
    _install_fake_ngspice,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Every supplied top-level `measurements` that is not an array, with the
#: Python type name the shared validator reports. The falsy ones (`null`, "",
#: {}, false, 0) are the shapes the old `or []` quietly turned into "no
#: measurements"; the truthy scalars (5, 1.5, true) are what raised.
_BAD_CONTAINERS = [
    pytest.param(5, "int", id="int"),
    pytest.param(1.5, "float", id="float"),
    pytest.param(True, "bool", id="true"),
    pytest.param(None, "NoneType", id="null"),
    pytest.param("abc", "str", id="string"),
    pytest.param("", "str", id="empty-string"),
    pytest.param({}, "dict", id="empty-object"),
    pytest.param(False, "bool", id="false"),
    pytest.param(0, "int", id="zero"),
]

#: An array holding a non-object entry: the offending index and Python type.
_BAD_ENTRIES = [
    pytest.param(["notadict"], 0, "str", id="string-entry"),
    pytest.param([None], 0, "NoneType", id="null-entry"),
    pytest.param([[]], 0, "list", id="array-entry"),
    pytest.param([{"name": "ok"}, "bad"], 1, "str", id="bad-entry-after-valid"),
    pytest.param([{}, 7], 1, "int", id="int-entry-after-empty-object"),
]


def _forbid_ngspice(monkeypatch) -> list[object]:
    """Stub the simulator so any launch fails the test, and record launches.

    The refusal must happen while the request loads, before a deck is built
    or ngspice started -- a real launch here means the guard sits too late.
    """
    launched: list[object] = []

    def fail(*args, **kwargs):
        launched.append(args)
        raise AssertionError("ngspice must not run for a malformed request")

    monkeypatch.setattr(op_sanity.subprocess, "run", fail)
    return launched


def _bad_request(tmp_path, measurements):
    return _write_request(tmp_path, _GOOD_NETLIST, measurements=measurements)


# --------------------------------------------------------------------------- #
# Library: OpSanityError out of load_request / run_op_sanity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("measurements", "type_name"), _BAD_CONTAINERS)
def test_load_request_refuses_a_malformed_measurements_container(
    tmp_path, measurements, type_name
):
    request = _bad_request(tmp_path, measurements)

    with pytest.raises(op_sanity.OpSanityError) as excinfo:
        op_sanity.load_request(str(request))

    assert str(excinfo.value) == (
        f"request.measurements must be an array of objects (got {type_name})"
    )


@pytest.mark.parametrize(("measurements", "index", "type_name"), _BAD_ENTRIES)
def test_load_request_refuses_a_non_object_measurement_entry(
    tmp_path, measurements, index, type_name
):
    request = _bad_request(tmp_path, measurements)

    with pytest.raises(op_sanity.OpSanityError) as excinfo:
        op_sanity.load_request(str(request))

    assert str(excinfo.value) == (
        f"measurements[{index}] must be an object (got {type_name})"
    )


@pytest.mark.parametrize(
    "measurements",
    [pytest.param(p.values[0], id=p.id) for p in (*_BAD_CONTAINERS, *_BAD_ENTRIES)],
)
def test_run_op_sanity_refuses_before_launching_ngspice(
    tmp_path, monkeypatch, measurements
):
    request = _bad_request(tmp_path, measurements)
    launched = _forbid_ngspice(monkeypatch)

    with pytest.raises(op_sanity.OpSanityError, match="measurements"):
        op_sanity.run_op_sanity(str(request))

    assert launched == []


def test_the_refusal_is_not_a_sim_error(tmp_path):
    """The lint's own error type is what the CLI handler and `klt eval`'s
    `op-sanity` gate catch; the shared validator's `SimError` must not leak
    through them uncaught."""
    from klayout_tools.sim_errors import SimError

    request = _bad_request(tmp_path, 5)

    with pytest.raises(op_sanity.OpSanityError) as excinfo:
        op_sanity.load_request(str(request))

    assert not isinstance(excinfo.value, SimError)


# --------------------------------------------------------------------------- #
# CLI: the shared error envelope, no traceback, no simulator launch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("measurements", "type_name"), _BAD_CONTAINERS)
def test_cli_op_lint_malformed_container_emits_the_error_envelope(
    tmp_path, monkeypatch, capsys, measurements, type_name
):
    request = _bad_request(tmp_path, measurements)
    launched = _forbid_ngspice(monkeypatch)

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "sim"
    assert envelope["error"]["message"] == (
        f"request.measurements must be an array of objects (got {type_name})"
    )
    assert launched == []


@pytest.mark.parametrize(("measurements", "index", "type_name"), _BAD_ENTRIES)
def test_cli_op_lint_non_object_entry_emits_the_error_envelope(
    tmp_path, monkeypatch, capsys, measurements, index, type_name
):
    request = _bad_request(tmp_path, measurements)
    launched = _forbid_ngspice(monkeypatch)

    assert main(["sim", "--op-lint", str(request), "--format", "json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["error"]["command"] == "sim"
    assert envelope["error"]["message"] == (
        f"measurements[{index}] must be an object (got {type_name})"
    )
    assert launched == []


def test_cli_op_lint_text_format_names_the_offending_path(
    tmp_path, monkeypatch, capsys
):
    request = _bad_request(tmp_path, [{"name": "ok"}, "bad"])
    _forbid_ngspice(monkeypatch)

    assert main(["sim", "--op-lint", str(request), "--format", "text"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "measurements[1] must be an object (got str)" in captured.err


# --------------------------------------------------------------------------- #
# Positive controls: what stays accepted
# --------------------------------------------------------------------------- #


def _clean_run(tmp_path, monkeypatch, **extra):
    request = _write_request(tmp_path, _GOOD_NETLIST, **extra)
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})
    return op_sanity.run_op_sanity(str(request))


def test_absent_measurements_stay_accepted(tmp_path, monkeypatch):
    report = _clean_run(tmp_path, monkeypatch)

    assert report["status"] == "clean"


def test_an_empty_measurements_array_stays_accepted(tmp_path, monkeypatch):
    report = _clean_run(tmp_path, monkeypatch, measurements=[])

    assert report["status"] == "clean"


def test_a_netlist_only_request_without_analysis_still_works(tmp_path, monkeypatch):
    """`analysis` stays optional: the guard checks the measurement container
    and its entry types only, never the sim-side analysis requirements."""
    request = tmp_path / "request.json"
    (tmp_path / "body.spice").write_text(_GOOD_NETLIST)
    request.write_text(json.dumps({"netlist": "body.spice"}))
    _install_fake_ngspice(monkeypatch, {"XM1": _SATURATED_NMOS, "XM2": _SATURATED_PMOS})

    report = op_sanity.run_op_sanity(str(request))

    assert report["status"] == "clean"


def test_valid_measurements_still_feed_the_node_reference_check(tmp_path, monkeypatch):
    report = _clean_run(
        tmp_path,
        monkeypatch,
        measurements=[
            {"name": "vout", "spice": ".meas tran vout FIND v(vout_node) AT=1n"},
            {"name": "vx", "expr": "v(out) - v(also_missing)"},
        ],
    )

    assert sorted(f["node"] for f in _findings(report, "missing_node")) == [
        "also_missing",
        "vout_node",
    ]


def test_step_scoped_measurements_are_still_scanned(tmp_path, monkeypatch):
    """The guard is top-level only: a step's own `measurements` keeps the
    lenient scan (issue #6412 owns that path's validation)."""
    report = _clean_run(
        tmp_path,
        monkeypatch,
        analysis_steps=[
            {
                "name": "bias",
                "analysis": {"kind": "op"},
                "measurements": [
                    {"name": "v", "expr": "v(step_missing)"},
                    "not-an-object",
                ],
            }
        ],
    )

    (finding,) = _findings(report, "missing_node")
    assert finding["node"] == "step_missing"
