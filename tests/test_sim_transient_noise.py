"""``options.transient_noise_seed`` and the transient-noise reproducibility
disclosure (issue #5772). Split from ``test_sim.py`` so that file stays under
its per-file size ceiling."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import sim
from klayout_tools import sim_transient_noise as sim_tn


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


def _noise_mc_request(tmp_path, body: str, mc: dict | None = None):
    (tmp_path / "body.spice").write_text(body)
    req = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
    }
    if mc is not None:
        req["monte_carlo"] = mc
    return _write_request(tmp_path, req)


# --------------------------------------------------------------------------- #
# Transient-noise reproducibility note + `options.transient_noise_seed` (#5772)
# --------------------------------------------------------------------------- #


def test_run_sim_tran_without_mc_discloses_transient_noise_reproducibility(
    tmp_path, monkeypatch
):
    """A run with no `monte_carlo` still gets the #5772 reproducibility note
    on `environment.transient_noise`: `trnoise` is not replayable by any
    known route, `trrandom` is replayable only via a `.control` `setseed`.
    No `seed` echo -- the request declares none."""
    request = _noise_mc_request(
        tmp_path,
        "Vn a 0 trnoise(1m 1n 0 0)\nVr b 0 trrandom(2 1n 1m)\nR1 a b 1k\n",
    )
    _stub_subprocess_run(monkeypatch)

    tn = sim.run_sim(str(request))["environment"]["transient_noise"]

    assert tn["reproducibility_note"] == (
        "trnoise streams are not replayable through any known ngspice "
        "seeding route (measured, issues #3957/#5772): re-running this "
        "request redraws them. trrandom streams are replayable only via "
        "a `.control` `setseed` command; this run declares no "
        "options.transient_noise_seed, so its trrandom stream is not "
        "reproducible either."
    )
    assert "seed" not in tn


def test_run_sim_tran_trnoise_only_note_says_nothing_about_trrandom(
    tmp_path, monkeypatch
):
    """The note is keyed on the kinds actually found: a `trnoise`-only run
    is told its streams are not replayable, without trrandom claims."""
    request = _noise_mc_request(tmp_path, "Vn a 0 trnoise(1m 1n 0 0)\nR1 a 0 1k\n")
    _stub_subprocess_run(monkeypatch)

    note = sim.run_sim(str(request))["environment"]["transient_noise"][
        "reproducibility_note"
    ]

    assert "trnoise" in note and "not replayable" in note
    assert "trrandom" not in note


def test_run_sim_transient_noise_seed_is_echoed_and_emitted_in_deck(
    tmp_path, monkeypatch
):
    """`options.transient_noise_seed` is echoed as
    `environment.transient_noise.seed` and emitted as `setseed N`, the first
    command of every corner deck's `.control` block (#5772)."""
    (tmp_path / "body.spice").write_text("Vn a 0 trrandom(2 1n 1m)\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"transient_noise_seed": 42, "keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    tn = report["environment"]["transient_noise"]
    assert tn["seed"] == 42
    assert "setseed 42" in tn["reproducibility_note"]
    (corner,) = report["corners"]
    deck_path = os.path.join(os.path.dirname(corner["artifacts"]["log"]), "corner.cir")
    deck_lines = Path(deck_path).read_text().splitlines()
    assert deck_lines[deck_lines.index(".control") + 1] == "setseed 42"


def test_run_sim_transient_noise_seed_note_never_claims_trnoise_replayable(
    tmp_path, monkeypatch
):
    """With `trnoise` and `trrandom` in the same netlist, a declared seed
    pins only the `trrandom` stream -- the note must keep saying `trnoise`
    is not replayable (#5772 edge case)."""
    (tmp_path / "body.spice").write_text(
        "Vn a 0 trnoise(1m 1n 0 0)\nVr b 0 trrandom(2 1n 1m)\nR1 a b 1k\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"transient_noise_seed": 7},
        },
    )
    _stub_subprocess_run(monkeypatch)

    tn = sim.run_sim(str(request))["environment"]["transient_noise"]

    note = tn["reproducibility_note"]
    assert "trnoise" in note and "not replayable" in note
    assert "setseed 7" in note


def test_run_sim_transient_noise_seed_rejects_non_integer(tmp_path):
    """A non-integer seed is an application error naming the option."""
    (tmp_path / "body.spice").write_text("Vn a 0 trrandom(2 1n 1m)\nR1 a 0 1k\n")
    for bad in ("42", 4.2, True, [42]):
        request = _write_request(
            tmp_path,
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "options": {"transient_noise_seed": bad},
            },
            name=f"request-{bad!r}.json",
        )
        with pytest.raises(sim.SimError, match="transient_noise_seed"):
            sim.run_sim(str(request))


def test_run_sim_transient_noise_seed_refused_for_xyce(tmp_path):
    """`setseed` is an ngspice `.control` command; the Xyce deck has no
    control block, so the option is refused by name rather than silently
    dropped."""
    (tmp_path / "body.spice").write_text("Vn a 0 trrandom(2 1n 1m)\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"transient_noise_seed": 42},
        },
    )
    with pytest.raises(sim.SimError, match="transient_noise_seed"):
        sim.run_sim(str(request))


def test_transient_noise_note_covers_steps_tran(tmp_path):
    """The note rides the same `analysis_steps[]` tran gate as the rest of
    the #2985 block (`_analysis_runs_tran`, not just scalar `tran`)."""
    (tmp_path / "body.spice").write_text("Vn a 0 trnoise(1m 1n 0 0)\nR1 a 0 1k\n")
    tn = sim_tn.transient_noise_environment(
        str(tmp_path / "body.spice"),
        request={"netlist": "body.spice"},
        analysis={"kind": "steps", "steps": [{"kind": "tran", "args": "1n 1u"}]},
        engine="ngspice",
        repo_root=str(tmp_path),
    )

    assert "reproducibility_note" in tn["transient_noise"]


def test_run_sim_transient_noise_seed_no_noise_source_environment_unchanged(
    tmp_path, monkeypatch
):
    """A declared seed does not create a `transient_noise` block for a run
    with no `trnoise`/`trrandom` source."""
    (tmp_path / "body.spice").write_text("V1 a 0 1\nR1 a 0 1k\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"transient_noise_seed": 42},
        },
    )
    _stub_subprocess_run(monkeypatch)

    assert "transient_noise" not in sim.run_sim(str(request))["environment"]


def test_transient_noise_seed_changes_checkpoint_fingerprint():
    """A resumed run must not reuse pre-#5772 (or different-seed) corners:
    the seed changes deck bytes, so it keys the checkpoint fingerprint."""
    kwargs = {
        "netlist_path": __file__,
        "models_lib": None,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "corner_points": [sim.CornerPoint(None, {}, 27)],
        "timeout_s": 5.0,
        "engine": "ngspice",
    }
    default = sim._checkpoint_fingerprint(**kwargs)
    assert sim._checkpoint_fingerprint(**kwargs, transient_noise_seed=None) == default
    assert sim._checkpoint_fingerprint(**kwargs, transient_noise_seed=42) != default
    assert sim._checkpoint_fingerprint(
        **kwargs, transient_noise_seed=43
    ) != sim._checkpoint_fingerprint(**kwargs, transient_noise_seed=42)


# --------------------------------------------------------------------------- #
# Off-host runner echo check (`check_transient_noise_seed_echo`, #5772)
# --------------------------------------------------------------------------- #


def test_seed_echo_check_passes_on_match_and_when_undeclared_or_inert():
    declared = {"options": {"transient_noise_seed": 42}}
    sim_tn.check_transient_noise_seed_echo(
        declared, {"environment": {"transient_noise": {"seed": 42}}}
    )
    # Undeclared: nothing to check, whatever the report carries.
    sim_tn.check_transient_noise_seed_echo({"options": {}}, {"environment": {}})
    sim_tn.check_transient_noise_seed_echo(
        {"options": {}}, {"environment": {"transient_noise": {"seed": 1}}}
    )
    # Declared but no qualifying transient-noise source: inert by design.
    sim_tn.check_transient_noise_seed_echo(declared, {"environment": {}})
    sim_tn.check_transient_noise_seed_echo(
        declared, {"environment": {"transient_noise": {}}}
    )


def test_seed_echo_check_fails_on_missing_or_different_echo():
    declared = {"options": {"transient_noise_seed": 42}}
    # A non-empty block with no `seed`: the runner predates the option and
    # dropped it after scanning the same sources.
    with pytest.raises(sim.SimError, match="transient_noise_seed"):
        sim_tn.check_transient_noise_seed_echo(
            declared,
            {"environment": {"transient_noise": {"engine": "ngspice"}}},
        )
    with pytest.raises(sim.SimError, match="declared 42, runner echoed 7"):
        sim_tn.check_transient_noise_seed_echo(
            declared, {"environment": {"transient_noise": {"seed": 7}}}
        )
