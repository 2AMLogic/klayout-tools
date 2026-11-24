"""Tests for ``klt pex --dry-run`` (issue #5464).

Every case here is engine-free: a dry run never extracts and never
simulates, so none of these need ngspice. The message-parity cases *do*
drive a real ``run_pex`` for comparison -- extraction of the tiny synthetic
resistor fixture is cheap, and the whole point of the criterion is that a
dry run's error is the real run's error rather than a lookalike.

The fixtures are imported from ``tests/test_pex.py`` so the dry run is
checked against the same layout/testbench/DUT shapes the real ``klt pex``
tests use.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools import pex as pex_module
from klayout_tools import pex_dry_run as dry_module
from klayout_tools.cli import main
from klayout_tools.pex import PexError, run_pex
from klayout_tools.pex_dry_run import run_pex_dry_run
from test_pex import (
    _make_sky130_poly_resistor_layout,
    _write_gds,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)

#: Report keys every dry run emits, whatever the mode.
_REPORT_KEYS = {
    "schema_version",
    "dry_run",
    "status",
    "layout",
    "deck",
    "top",
    "deck_options",
    "declared_pins",
    "legs",
    "reference_netlist",
    "testbenches",
    "not_checked",
}


@pytest.fixture
def bench(tmp_path):
    """A valid ``klt pex`` invocation's inputs: layout, schematic DUT,
    testbench body, and the `klt sim` request that `.include`s it."""
    layout = _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    testbench = _write_testbench(tmp_path / "tb.spice", dut)
    request = _write_request(tmp_path / "req.json", testbench)
    return {
        "root": tmp_path,
        "layout": str(layout),
        "dut": str(dut),
        "testbench": str(testbench),
        "request": str(request),
    }


def _tree(root: Path) -> set[str]:
    """Every path under ``root``, relative -- for "wrote nothing" asserts."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# --------------------------------------------------------------------------- #
# The valid case
# --------------------------------------------------------------------------- #


def test_dry_run_reports_the_expansion_and_writes_nothing(bench):
    before = _tree(bench["root"])
    outdir = bench["root"] / "artifacts"

    report = run_pex_dry_run(
        bench["layout"],
        [bench["request"]],
        "sky130",
    )

    assert set(report) == _REPORT_KEYS
    assert report["dry_run"] is True
    assert report["status"] == "valid"
    assert report["deck"] == "sky130"
    assert report["top"] == "RES"
    assert report["legs"] == ["schematic", "extracted"]
    assert report["deck_options"] == {}
    assert report["declared_pins"] is None
    # `{path, scope}`, the same shape the full `klt pex` report uses.
    assert set(report["layout"]) == {"path", "scope"}
    assert set(report["reference_netlist"]) == {"path", "scope"}

    (entry,) = report["testbenches"]
    assert set(entry) == {"request", "schematic_netlist", "sim"}
    assert set(entry["request"]) == {"path", "scope"}
    # The nested `klt sim --dry-run` report, unmodified.
    assert entry["sim"]["dry_run"] is True
    assert entry["sim"]["status"] == "valid"
    assert entry["sim"]["unit_count"] >= 1
    assert entry["sim"]["units"]

    assert report["not_checked"] == [
        "extraction",
        "netlist_diagnostics",
        "extracted_leg_preflight",
    ]

    # Nothing extracted, nothing simulated, nothing written -- not beside the
    # layout (`.klt/pex/`), not at `-o`, not in `--outdir`.
    assert _tree(bench["root"]) == before
    assert not outdir.exists()


def test_dry_run_takes_no_output_directory_lock(bench, monkeypatch):
    """A dry run never calls ``hold_output_dir`` (issue #2770's lock), so a
    concurrent cooperating run on the same root is not refused."""
    from klayout_tools import output_lock

    calls: list[str] = []
    real = output_lock.hold_output_dir
    monkeypatch.setattr(
        output_lock,
        "hold_output_dir",
        lambda path, *a, **k: (calls.append(path), real(path, *a, **k))[1],
    )
    run_pex_dry_run(bench["layout"], [bench["request"]], "sky130")
    assert calls == []


def test_dry_run_never_extracts(bench, monkeypatch):
    """The edge case from the issue's test plan: a dry run reaches its
    verdict without ``run_extract`` ever being called."""

    def _explode(*args, **kwargs):  # pragma: no cover -- must never run
        raise AssertionError("run_extract must not be called by a dry run")

    monkeypatch.setattr(pex_module, "run_extract", _explode)
    report = run_pex_dry_run(bench["layout"], [bench["request"]], "sky130")
    assert report["status"] == "valid"


def test_dry_run_simulates_nothing(bench, monkeypatch):
    """Every nested ``run_sim`` call is a dry run; none dispatches."""
    seen: list[dict] = []
    real = dry_module.run_sim

    def _spy(request, **kwargs):
        seen.append(kwargs)
        return real(request, **kwargs)

    monkeypatch.setattr(dry_module, "run_sim", _spy)
    run_pex_dry_run(bench["layout"], [bench["request"]], "sky130")
    assert seen and all(kwargs["dry_run"] is True for kwargs in seen)


def test_dry_run_with_batch_backend_makes_no_submission(bench, monkeypatch):
    """`--backend batch` rides the nested `klt sim --dry-run` contract: the
    unit list is expanded, no job is submitted."""
    from klayout_tools import sim_batch

    def _explode(*args, **kwargs):  # pragma: no cover -- must never run
        raise AssertionError("a dry run must not submit a batch job")

    for name in ("submit_batch_job", "run_batch_units"):
        if hasattr(sim_batch, name):
            monkeypatch.setattr(sim_batch, name, _explode)

    report = run_pex_dry_run(
        bench["layout"], [bench["request"]], "sky130", backend="batch"
    )
    assert report["status"] == "valid"
    assert report["testbenches"][0]["sim"]["backend"] == "batch"


def test_dry_run_device_only_leg_adds_the_third_leg(bench):
    report = run_pex_dry_run(
        bench["layout"], [bench["request"]], "sky130", device_only_leg=True
    )
    assert report["legs"] == ["schematic", "extracted", "device-only"]


def test_dry_run_echoes_declared_pins(bench):
    report = run_pex_dry_run(
        bench["layout"],
        [bench["request"]],
        "sky130",
        declared_pins=frozenset({"RB", "RA"}),
    )
    assert report["declared_pins"] == ["RA", "RB"]


# --------------------------------------------------------------------------- #
# `not_checked[]`
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"tie_nets": [("\\$5", "VSUB")]}, "tie_nets"),
        ({"flatten_schematic": True}, "flatten_schematic"),
    ],
)
def test_not_checked_names_each_unverifiable_flag(bench, kwargs, code):
    report = run_pex_dry_run(bench["layout"], [bench["request"]], "sky130", **kwargs)
    assert code in report["not_checked"]
    # The always-on codes are still there, first and in order.
    assert report["not_checked"][:3] == [
        "extraction",
        "netlist_diagnostics",
        "extracted_leg_preflight",
    ]


# --------------------------------------------------------------------------- #
# The other two input modes
# --------------------------------------------------------------------------- #


def test_dry_run_extract_only_has_no_testbenches_and_no_legs(bench):
    report = run_pex_dry_run(bench["layout"], [], "sky130", extract_only=True)
    assert report["testbenches"] == []
    assert report["legs"] == []
    assert report["reference_netlist"]["path"] is None
    assert report["top"] == "RES"


def test_dry_run_measure_command_resolves_the_reference_without_running_it(bench):
    """`--measure-command` is validated and reported, never executed: the
    command here would leave a marker file if it ever ran."""
    marker = bench["root"] / "command-ran"
    report = run_pex_dry_run(
        bench["layout"],
        [],
        "sky130",
        measure_command=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
        reference_netlist=bench["dut"],
    )
    assert not marker.exists()
    assert report["testbenches"] == []
    assert "measure_command" in report["not_checked"]


def test_dry_run_measure_command_refuses_a_reference_with_no_circuit(bench):
    """The same Gap-1 check (issue #1255) the real run applies, with the
    same message."""
    params = bench["root"] / "corners.spice"
    params.write_text(".param vdd=1.8\n")
    with pytest.raises(PexError, match="declares no `.SUBCKT`"):
        run_pex_dry_run(
            bench["layout"],
            [],
            "sky130",
            measure_command=["true"],
            reference_netlist=str(params),
        )


# --------------------------------------------------------------------------- #
# `--submit-only` / `--attach` refusal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"submit_only": True, "backend": "batch"}, "--submit-only"),
        ({"attach": "pex-submit.json"}, "--attach"),
    ],
)
def test_dry_run_refuses_the_async_forms(bench, kwargs, flag):
    with pytest.raises(PexError) as excinfo:
        run_pex_dry_run(bench["layout"], [bench["request"]], "sky130", **kwargs)
    assert f"--dry-run cannot be combined with {flag}" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Message parity with the non-dry invocation
# --------------------------------------------------------------------------- #


def _message(fn, bench, testbenches, deck="sky130", **kwargs):
    """``fn``'s :class:`PexError` message for this invocation."""
    extra = {} if fn is run_pex_dry_run else {"output": str(bench["root"] / "o.spice")}
    with pytest.raises(PexError) as excinfo:
        fn(bench["layout"], testbenches, deck, **kwargs, **extra)
    return str(excinfo.value)


def _assert_same_message(bench, testbenches, deck="sky130", **kwargs):
    dry = _message(run_pex_dry_run, bench, testbenches, deck, **kwargs)
    real = _message(run_pex, bench, testbenches, deck, **kwargs)
    assert dry == real
    return dry


def test_unknown_deck_message_matches(bench):
    assert "unknown deck" in _assert_same_message(
        bench, [bench["request"]], deck="not-a-deck"
    )


def test_invalid_deck_option_message_matches(bench):
    _assert_same_message(bench, [bench["request"]], deck_options={"no_such_key": "1"})


def test_invalid_label_layer_message_matches(bench):
    _assert_same_message(
        bench, [bench["request"]], label_layers={"no_such_role": (67, 5)}
    )


def test_missing_layout_message_matches(bench):
    bench["layout"] = str(bench["root"] / "absent.gds")
    assert "file not found" in _assert_same_message(bench, [bench["request"]])


def test_unreadable_layout_message_matches(bench):
    bench["layout"] = str(bench["root"] / "junk.gds")
    Path(bench["layout"]).write_text("not a layout stream\n")
    assert "could not read layout" in _assert_same_message(bench, [bench["request"]])


def test_bad_wait_for_capacity_message_matches(bench):
    """A `run_pex` pre-extraction refusal that is not a measure-mode rule."""
    _assert_same_message(
        bench, [bench["request"]], backend="batch", capacity_wait_s=-1.0
    )


def test_ambiguous_top_message_matches(bench, tmp_path):
    """Two top cells and no ``--top``: the same refusal, before any
    extraction."""
    layout = _make_sky130_poly_resistor_layout()
    layout.create_cell("SECOND")
    bench["layout"] = str(_write_gds(layout, tmp_path / "two-tops.gds"))
    _assert_same_message(bench, [bench["request"]])


def test_unknown_top_message_matches(bench):
    _assert_same_message(bench, [bench["request"]], top="NOPE")


@pytest.mark.parametrize("flag", ["rename_declared_pins", "tie_declared_pins"])
def test_declared_pin_set_required_message_matches(bench, flag):
    assert "requires a declared pin set" in _assert_same_message(
        bench, [bench["request"]], **{flag: True}
    )


def test_resistance_graph_with_distributed_rc_message_matches(bench):
    assert "--resistance-graph cannot be combined" in _assert_same_message(
        bench,
        [bench["request"]],
        critical_nets=["RA"],
        resistance_graph=True,
        distributed_rc=True,
    )


def test_extract_only_flag_conflict_message_matches(bench):
    """A `run_pex` flag-combination refusal made before extraction."""
    _assert_same_message(bench, [bench["request"]], extract_only=True)


def test_measure_command_without_reference_message_matches(bench):
    _assert_same_message(bench, [], measure_command=["true"])


def test_unloadable_testbench_request_message_matches(bench):
    broken = bench["root"] / "broken.json"
    broken.write_text("{not json")
    _assert_same_message(bench, [str(broken)])


def test_missing_testbench_body_message_matches(bench):
    orphan = bench["root"] / "orphan.json"
    orphan.write_text(
        json.dumps(
            {
                "netlist": str(bench["root"] / "gone.spice"),
                "analysis": {"kind": "op"},
            }
        )
    )
    assert "netlist not found" in _assert_same_message(bench, [str(orphan)])


def test_testbench_without_include_message_matches(bench):
    body = bench["root"] / "flat.spice"
    body.write_text("R1 RA RB 289.2\nVdd RA 0 DC 1.8\n")
    request = _write_request(bench["root"] / "flat.json", body)
    assert "`.include`" in _assert_same_message(bench, [str(request)])


def test_testbench_with_two_includes_message_matches(bench):
    body = bench["root"] / "two.spice"
    body.write_text(f'.include "{bench["dut"]}"\n.include "{bench["dut"]}"\n')
    request = _write_request(bench["root"] / "two.json", body)
    _assert_same_message(bench, [str(request)])


def test_include_target_without_a_circuit_message_matches(bench):
    params = bench["root"] / "params.spice"
    params.write_text(".param vdd=1.8\n")
    body = bench["root"] / "paramtb.spice"
    body.write_text(f'.include "{params}"\nVdd RA 0 DC 1.8\n')
    request = _write_request(bench["root"] / "paramtb.json", body)
    assert "declares no `.SUBCKT`" in _assert_same_message(bench, [str(request)])


def test_testbenches_disagreeing_on_the_dut_message_matches(bench):
    other_dut = _write_schematic_dut(bench["root"] / "other.spice", r_ohm=100.0)
    other_tb = _write_testbench(bench["root"] / "other_tb.spice", other_dut)
    other_request = _write_request(bench["root"] / "other_req.json", other_tb)
    assert "different schematic DUT" in _assert_same_message(
        bench, [bench["request"], str(other_request)]
    )


def test_nested_sim_dry_run_failure_is_a_pex_error(bench):
    """A request `klt sim --dry-run` itself refuses fails the pex dry run."""
    request = bench["root"] / "bad-analysis.json"
    request.write_text(
        json.dumps({"netlist": bench["testbench"], "analysis": {"kind": "nope"}})
    )
    with pytest.raises(PexError) as excinfo:
        run_pex_dry_run(bench["layout"], [str(request)], "sky130")
    assert str(request) in str(excinfo.value)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def _argv(bench, *extra, deck="sky130"):
    return [
        "pex",
        bench["layout"],
        bench["request"],
        "--deck",
        deck,
        "--dry-run",
        *extra,
    ]


def test_cli_dry_run_json_envelope(bench, capsys):
    exit_code = main(_argv(bench, "--format", "json"))
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 2
    assert report["dry_run"] is True
    assert report["status"] == "valid"


def test_cli_dry_run_text_output(bench, capsys):
    exit_code = main(_argv(bench))
    assert exit_code == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[0].startswith("klt pex --dry-run: valid (1 testbench(es), ")
    assert "legs=schematic+extracted" in lines[0]
    assert lines[1:] == [
        "  not checked: extraction",
        "  not checked: netlist_diagnostics",
        "  not checked: extracted_leg_preflight",
    ]


def test_cli_dry_run_writes_no_envelope_out(bench, capsys):
    envelope = bench["root"] / "envelope.json"
    exit_code = main(_argv(bench, "--envelope-out", str(envelope), "--format", "json"))
    capsys.readouterr()
    assert exit_code == 0
    assert not envelope.exists()


def test_cli_dry_run_validation_failure_exits_1(bench, capsys):
    exit_code = main(_argv(bench, "--format", "json", deck="not-a-deck"))
    assert exit_code == 1
    # The documented error envelope goes to stderr (see cli/output.py).
    envelope = json.loads(capsys.readouterr().err)
    assert envelope["error"]["command"] == "pex"
    assert "unknown deck" in envelope["error"]["message"]
