"""``models.param_file`` (issue #4759): resolved like ``models.lib`` and
``.include``d ahead of the ``.lib`` cards of every generated deck."""

import subprocess
from pathlib import Path

import pytest

import test_op_sanity as _ops
from klayout_tools import op_sanity, sim


def _deck(tmp_path: Path, point) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib="/m/models.lib",
        point=point,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[],
        raw_path=None,
    )
    return deck_path.read_text().splitlines()


def test_unset_leaves_lib_lines_unchanged():
    point = sim.CornerPoint("tt", {}, 27)
    assert sim._corner_lib_lines(point, "/m/models.lib") == [".lib /m/models.lib tt"]


def test_include_precedes_lib_cards(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    point.param_file = "/m/design.ngspice"
    lines = _deck(tmp_path, point)
    inc = lines.index(".include /m/design.ngspice")
    assert inc < lines.index(".lib /m/models.lib tt")


def test_include_precedes_multi_section_libs():
    point = sim.CornerPoint("typ", {}, 27, process_sections=["nmos_t", "pmos_t"])
    point.param_file = "/m/design.ngspice"
    assert sim._corner_lib_lines(point, "/m/models.lib") == [
        ".include /m/design.ngspice",
        ".lib /m/models.lib nmos_t",
        ".lib /m/models.lib pmos_t",
    ]


def test_xyce_deck_has_include_first(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    point.param_file = "/m/design.ngspice"
    deck_path = tmp_path / "x.cir"
    sim._write_xyce_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib="/m/models.lib",
        point=point,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[],
    )
    lines = deck_path.read_text().splitlines()
    assert lines.index(".include /m/design.ngspice") < lines.index(
        ".lib /m/models.lib tt"
    )


def test_resolve_sets_every_point_relative_to_request_dir(tmp_path):
    (tmp_path / "design.ngspice").write_text(".param fnoicor=1\n")
    points = [sim.CornerPoint("tt", {}, 27), sim.CornerPoint("ss", {}, 27)]
    path = sim._resolve_param_file(
        points, {"param_file": "design.ngspice"}, str(tmp_path)
    )
    assert path == str(tmp_path / "design.ngspice")
    assert all(p.param_file == path for p in points)


def test_resolve_unset_is_none(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    assert sim._resolve_param_file([point], {}, str(tmp_path)) is None
    assert point.param_file is None


def test_resolve_missing_file_is_a_sim_error(tmp_path):
    with pytest.raises(sim.SimError, match="model parameter file not found"):
        sim._resolve_param_file(
            [sim.CornerPoint("tt", {}, 27)], {"param_file": "nope"}, str(tmp_path)
        )


# --------------------------------------------------------------------------- #
# Batch staging of `models.param_file` (kept out of test_sim_staging.py for the
# file-size ratchet)
# --------------------------------------------------------------------------- #


def _stage_job(tmp_path: Path, request: dict):
    from klayout_tools import sim_staging as st

    netlist = tmp_path / "tb.spice"
    netlist.write_text("* tb\n")
    return st.stage_sim_job(
        request,
        str(netlist),
        request_dir=str(tmp_path),
        netlist_staged_name="netlist.cir",
        reserved_names=("request.json",),
        backend="batch",
    )


def test_param_file_is_staged_and_rewritten(tmp_path):
    (tmp_path / "m.lib").write_text("* lib\n")
    (tmp_path / "design.ngspice").write_text(".param fnoicor=1\n")
    request = {
        "models": {"lib": "m.lib", "param_file": "design.ngspice"},
        "options": {"stage_model_inputs": True},
    }

    job = _stage_job(tmp_path, request)

    staged_param = job.request["models"]["param_file"]
    assert not Path(staged_param).is_absolute()
    assert staged_param in {item.staged_name for item in job.model_files}


def test_param_file_missing_is_refused(tmp_path):
    (tmp_path / "m.lib").write_text("* lib\n")
    request = {
        "models": {"lib": "m.lib", "param_file": "nope.ngspice"},
        "options": {"stage_model_inputs": True},
    }

    with pytest.raises(Exception, match="not found"):
        _stage_job(tmp_path, request)


def test_param_file_reaches_the_op_lint_deck(tmp_path, monkeypatch):
    """Issue #4759: `models.param_file` must be `.include`d in the op-lint
    deck, ahead of the netlist (and any `.lib` cards), as in the `klt sim` decks."""
    param = tmp_path / "design.ngspice"
    param.write_text("* params\n")
    lib = tmp_path / "models.lib"
    lib.write_text("* lib\n")
    request = _ops._write_request(
        tmp_path,
        _ops._GOOD_NETLIST,
        models={"lib": str(lib), "param_file": str(param)},
    )
    decks: list[list[str]] = []
    real_run = subprocess.run
    _ops._install_fake_ngspice(
        monkeypatch,
        {"XM1": _ops._SATURATED_NMOS, "XM2": _ops._SATURATED_PMOS},
        node_voltages={"vdd": 1.8, "in": 0.9, "bias": 0.6, "out": 1.2},
    )
    fake_run = op_sanity.subprocess.run

    def recording_run(cmd, **kwargs):
        if "-b" not in cmd:
            return real_run(cmd, **kwargs)
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text().splitlines())
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(op_sanity.subprocess, "run", recording_run)

    op_sanity.run_op_sanity(str(request))

    (deck,) = decks
    inc = deck.index(f".include {param}")
    body = next(i for i, line in enumerate(deck) if "body.spice" in line)
    assert inc < body < deck.index(".control")
