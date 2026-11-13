"""``klt sim`` wiring for ``measurements[].slope_band`` (issue #5518).

Request validation, deck emission, grading and the mandatory runner feature.
The run-search numerics themselves are tested in tests/test_sim_slope_band.py.

The stubbed-``ngspice`` tests feed a hand-built rawfile (cf.
tests/test_sim_snm_meas.py); the ``real_ngspice`` test runs the engine on a
behavioural tanh amplifier whose in-band run is known *analytically* --
``slope = 0.5*k*(1 - tanh^2(k*(x - 0.5)))``, so ``slope >= lo`` exactly where
``|x - 0.5| <= artanh(sqrt(1 - 2*lo/k))/k`` -- never from the rawfile the code
under test produced.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_batch, sim_slope_band, sim_slope_band_meas

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_BODY = {
    "y": "v(out)",
    "x": "v(in)",
    "anchor": 0.5,
    "min_slope": 1.0,
    "max_slope": 2.5,
    "report": "y_span",
}
_DECL = {"name": "usable_range", "slope_band": _BODY}

# A ramp that saturates at both ends: slope is exactly 1 for 0.3 < x < 1.7.
_XS = [i / 100 for i in range(201)]
_YS = [max(0.3, min(1.7, x)) for x in _XS]

# The behavioural amplifier the real-ngspice test sweeps, and its analytic run.
_K = 4.0
_MIN_SLOPE = 1.0
_HALF_WIDTH = math.atanh(math.sqrt(1.0 - 2.0 * _MIN_SLOPE / _K)) / _K
_EXPECTED_X_SPAN = 2.0 * _HALF_WIDTH
_EXPECTED_Y_SPAN = math.tanh(_K * _HALF_WIDTH)


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not-an-object", "must be an object"),
        ({**_BODY, "extra": 1}, "unknown slope_band key"),
        ({k: v for k, v in _BODY.items() if k != "anchor"}, "requires \\['anchor'\\]"),
        ({k: v for k, v in _BODY.items() if k != "report"}, "requires \\['report'\\]"),
        ({**_BODY, "x": 5}, "slope_band.x"),
        ({**_BODY, "y": "v(a b)"}, "slope_band.y"),
        ({**_BODY, "x": "i(v1)"}, "slope_band.x"),
        ({**_BODY, "y": "v()"}, "slope_band.y"),
        ({**_BODY, "anchor": "0.5"}, "slope_band.anchor must be a finite number"),
        ({**_BODY, "anchor": True}, "slope_band.anchor must be a finite number"),
        ({**_BODY, "min_slope": float("inf")}, "slope_band.min_slope"),
        ({**_BODY, "min_slope": 3.0}, "is above slope_band.max_slope"),
        ({**_BODY, "report": "slope"}, "slope_band.report must be one of"),
        ({**_BODY, "report": None}, "slope_band.report must be one of"),
    ],
)
def test_malformed_slope_band_is_rejected(body, message):
    with pytest.raises(sim.SimError, match=message):
        sim._validate_measurement_forms([{"name": "s", "slope_band": body}])


@pytest.mark.parametrize("other", sim_slope_band_meas._FORMS)
def test_conflicting_forms_are_rejected(other):
    spec = {"name": "s", "slope_band": _BODY, other: {"x": 1}}
    with pytest.raises(sim.SimError, match=f"'slope_band' together with '{other}'"):
        sim._validate_measurement_forms([spec])


def test_every_other_form_also_refuses_slope_band_beside_it():
    """Exclusivity must not depend on which validator happens to run first."""
    for other in ("snm", "eye", "settled", "step_resolution", "search", "transfer"):
        spec = {"name": "s", other: {"x": 1}, "slope_band": _BODY}
        with pytest.raises(sim.SimError, match="declare exactly one"):
            sim._validate_measurement_forms([spec])


def test_valid_slope_band_is_accepted_and_selectors_normalise():
    sim._validate_measurement_forms([_DECL])
    assert sim_slope_band_meas.normalize_selector("V(OUT)") == "v(out)"
    assert sim_slope_band_meas.normalize_selector(" v(a)") is None


def _write_body(tmp_path: Path) -> None:
    (tmp_path / "body.spice").write_text(
        f"Vin in 0 DC 0\nBout out 0 V = 0.5 + 0.5*tanh({_K}*(V(in)-0.5))\n"
    )


def _request(tmp_path: Path, measurements: list[dict], **extra) -> Path:
    _write_body(tmp_path)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "dc", "args": "Vin 0 1 0.0025"},
                "measurements": measurements,
                "options": {"keep_artifacts": False},
                **extra,
            }
        )
    )
    return path


@pytest.mark.parametrize(
    "kind,args", [("tran", "1n 1u"), ("ac", "dec 10 1 1e6"), ("op", "")]
)
def test_non_dc_analysis_is_rejected(tmp_path, kind, args):
    request = _request(tmp_path, [_DECL])
    doc = json.loads(request.read_text())
    doc["analysis"] = {"kind": kind, "args": args}
    request.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match='requires analysis.kind "dc"'):
        sim.run_sim(str(request))


def test_analysis_steps_is_rejected_by_name(tmp_path):
    _write_body(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis_steps": [
                    {
                        "name": "s1",
                        "analysis": {"kind": "dc", "args": "Vin 0 1 0.01"},
                        "measurements": [_DECL],
                    }
                ],
                "options": {"keep_artifacts": False},
            }
        )
    )
    with pytest.raises(sim.SimError, match="not supported with analysis_steps"):
        sim.run_sim(str(request))


def test_duplicate_names_are_rejected(tmp_path):
    other = {"name": "USABLE_RANGE", "slope_band": {**_BODY, "report": "x_span"}}
    request = _request(tmp_path, [_DECL, other])
    with pytest.raises(sim.SimError, match="duplicate measurement name"):
        sim.run_sim(str(request))


def test_refused_for_xyce(tmp_path):
    request = _request(tmp_path, [_DECL], engine="xyce")
    with pytest.raises(sim.SimError, match="slope_band is not supported for engine"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def _deck(tmp_path: Path, measurements_spec, **kwargs) -> list[str]:
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "dc", "args": "Vin 0 1 0.01"},
        measurements_spec=measurements_spec,
        raw_path=None,
        **kwargs,
    )
    return deck_path.read_text().splitlines()


def test_deck_dumps_each_unique_pair_once_after_the_analysis(tmp_path):
    specs = [
        _DECL,
        # Same pair, different report and band: one rawfile.
        {"name": "again", "slope_band": {**_BODY, "x": "V(IN)", "report": "x_span"}},
        {"name": "other", "slope_band": {**_BODY, "y": "v(out2)"}},
    ]
    lines = _deck(tmp_path, specs)
    dc = lines.index("dc Vin 0 1 0.01")
    assert lines.index("set filetype=ascii") < dc
    assert lines.count("let __klt_sband_0_x = v(in)") == 1
    assert "let __klt_sband_1_y = v(out2)" in lines
    write = lines.index(
        f"write {tmp_path / 'slope_band_0.raw'} __klt_sband_0_x __klt_sband_0_y"
    )
    assert dc < lines.index("let __klt_sband_0_x = v(in)") < write < lines.index("quit")
    assert not any("__klt_sband_2" in line for line in lines)


def test_deck_without_slope_band_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas dc v MAX v(out)"}])
    assert not any("__klt_sband" in line or "filetype" in line for line in lines)


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading, error tokens, response shape
# --------------------------------------------------------------------------- #


def _rawfile(xs, ys, *, nvars: int = 3, points: int | None = None) -> str:
    n = len(xs) if points is None else points
    names = ["v-sweep", "v(__klt_sband_0_x)", "v(__klt_sband_0_y)"]
    lines = [
        "Title: stub",
        "Plotname: DC transfer characteristic",
        "Flags: real",
        f"No. Variables: {nvars}",
        f"No. Points: {n}",
        "Variables:",
    ]
    lines += [f"\t{i}\t{name}\tvoltage" for i, name in enumerate(names[:nvars])]
    lines.append("Values:")
    for i, (x, y) in enumerate(zip(xs, ys, strict=True)):
        lines += [f" {i}\t{x!r}", f"\t{x!r}", f"\t{y!r}"][: nvars + 1]
    return "\n".join(lines) + "\n"


def _stub_ngspice(monkeypatch, *, raw: str | None):
    real_which = shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("No. of Data Rows : 5\n")
        for arg in cmd:
            if str(arg).endswith(".cir"):
                decks.append(Path(arg).read_text())
        if raw is not None:
            with open(
                os.path.join(cwd, "slope_band_0.raw"), "w", encoding="utf-8"
            ) as h:
                h.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


def _only_corner(report):
    (corner,) = report["corners"]
    return corner, {m["name"]: m for m in corner["measurements"]}


def test_value_is_graded_and_carries_the_full_run_breakdown(tmp_path, monkeypatch):
    request = _request(
        tmp_path, [{**_DECL, "unit": "V", "limits": {"min": 1.0}, "spec_row": "AC-3"}]
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS))

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    entry = by_name["usable_range"]
    assert entry["value"] == pytest.approx(1.38)
    assert entry["status"] == "pass"
    assert entry["margin"] == pytest.approx(0.38)
    assert entry["unit"] == "V"
    band = entry["slope_band"]
    # The sibling echoes all six request keys...
    assert {k: band[k] for k in _BODY} == _BODY
    # ...and carries every scalar plus both edge reasons and the run length.
    assert band["y_span"] == pytest.approx(1.38)
    assert band["x_lo"] == pytest.approx(0.31)
    assert band["x_hi"] == pytest.approx(1.69)
    assert (band["edge_lo_at"], band["edge_hi_at"]) == ("band", "band")
    assert band["points_in_run"] == 139
    assert corner["diagnostics"] == []
    # The rollup grades it like any other scalar.
    (rollup,) = report["measurements"]
    assert rollup["status"] == "pass"
    assert rollup["spec_row"] == "AC-3"
    assert rollup["worst_case"]["value"] == pytest.approx(1.38)


def test_a_failing_limit_fails_the_run(tmp_path, monkeypatch):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 1.5}}])
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS))

    report = sim.run_sim(str(request))

    _, by_name = _only_corner(report)
    assert by_name["usable_range"]["status"] == "fail"
    assert report["status"] == "fail"


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        (None, "[slope_band_vector_missing]"),
        ("garbage\n", "[slope_band_raw_invalid]"),
        (_rawfile(_XS, _YS, nvars=2), "[slope_band_raw_invalid]"),
        (_rawfile(_XS[:2], _YS[:2]), "[slope_band_invalid_data]"),
        (
            _rawfile([0.0, 1.0, 1.0, 2.0], [0.0, 1.0, 1.0, 2.0]),
            "[slope_band_non_monotonic]",
        ),
        (
            _rawfile([9.0, 9.1, 9.2], [0.0, 0.1, 0.2]),
            "[slope_band_anchor_outside_sweep]",
        ),
        (_rawfile(_XS, [0.0 for _ in _XS]), "[slope_band_anchor_out_of_band]"),
    ],
)
def test_failures_are_errors_with_a_reason_token_never_a_pass(
    tmp_path, monkeypatch, raw, token
):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 0.0}}])
    _stub_ngspice(monkeypatch, raw=raw)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    entry = by_name["usable_range"]
    assert entry["status"] == "error"
    assert entry["value"] is None
    assert "slope_band" not in entry
    (diag,) = corner["diagnostics"]
    assert diag["code"] == "measurement"
    assert diag["message"].startswith(token)
    assert report["status"] != "pass"


def test_entries_differing_only_by_report_share_one_rawfile_and_one_search(
    tmp_path, monkeypatch
):
    specs = [
        _DECL,
        {"name": "x_range", "slope_band": {**_BODY, "report": "x_span"}},
        {"name": "low_edge", "slope_band": {**_BODY, "report": "y_lo"}},
    ]
    request = _request(tmp_path, specs)
    decks = _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS))
    calls = []
    real = sim_slope_band.analyze_slope_band
    monkeypatch.setattr(
        sim_slope_band,
        "analyze_slope_band",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )

    report = sim.run_sim(str(request))

    _, by_name = _only_corner(report)
    assert by_name["usable_range"]["value"] == pytest.approx(1.38)
    assert by_name["x_range"]["value"] == pytest.approx(1.38)
    assert by_name["low_edge"]["value"] == pytest.approx(0.31)
    assert len(calls) == 1
    assert decks[0].count("write ") == 1


def test_monte_carlo_counts_an_errored_sample_rather_than_dropping_it(
    tmp_path, monkeypatch
):
    """An out-of-band anchor is `status: "error"`, and Monte Carlo accounts for
    it explicitly: `n + errored + inconclusive` is the number of samples drawn,
    so an error never silently shrinks the yield denominator."""
    request = _request(
        tmp_path,
        [{**_DECL, "limits": {"min": 1.0}}],
        monte_carlo={"n": 3, "seed": 5, "vary": "mismatch"},
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, [0.0 for _ in _XS]))

    report = sim.run_sim(str(request))

    (rollup,) = report["measurements"]
    stats = rollup["monte_carlo"]
    assert stats["n"] == 0
    assert stats["errored"] == 3
    assert stats["n"] + stats["errored"] + stats["inconclusive"] == 3
    assert rollup["status"] == "error"


def test_monte_carlo_statistics_carry_a_good_value(tmp_path, monkeypatch):
    request = _request(
        tmp_path,
        [{**_DECL, "limits": {"min": 1.0}}],
        monte_carlo={"n": 3, "seed": 5, "vary": "mismatch"},
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS))

    report = sim.run_sim(str(request))

    (rollup,) = report["measurements"]
    assert rollup["status"] == "pass"
    assert rollup["monte_carlo"]["n"] == 3
    assert rollup["monte_carlo"]["errored"] == 0
    assert rollup["monte_carlo"]["mean"] == pytest.approx(1.38)


# --------------------------------------------------------------------------- #
# Real ngspice
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_real_ngspice_tanh_amplifier_matches_the_analytic_run(tmp_path):
    specs = [
        {**_DECL, "unit": "V", "limits": {"min": 0.5}},
        {"name": "x_range", "slope_band": {**_BODY, "report": "x_span"}},
        {"name": "bad", "slope_band": {**_BODY, "y": "v(nope)"}},
    ]
    request = _request(tmp_path, specs)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    # Edges land on sweep points, so the measured span is short by at most one
    # 0.0025 V step at each end -- never long (no interpolation, by design).
    assert by_name["usable_range"]["value"] == pytest.approx(_EXPECTED_Y_SPAN, abs=0.01)
    assert by_name["usable_range"]["value"] <= _EXPECTED_Y_SPAN
    assert by_name["usable_range"]["status"] == "pass"
    assert by_name["x_range"]["value"] == pytest.approx(_EXPECTED_X_SPAN, abs=0.006)
    assert by_name["x_range"]["value"] <= _EXPECTED_X_SPAN
    band = by_name["usable_range"]["slope_band"]
    assert (band["edge_lo_at"], band["edge_hi_at"]) == ("band", "band")
    assert band["points_in_run"] > 100
    assert by_name["bad"]["status"] == "error"
    assert [d["message"][:27] for d in corner["diagnostics"]] == [
        "[slope_band_vector_missing]"
    ]


# --------------------------------------------------------------------------- #
# Mandatory batch runner feature
# --------------------------------------------------------------------------- #

_SLOPE_BAND_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [_DECL],
}


def test_slope_band_is_a_registered_mandatory_runner_feature():
    assert sim_batch._request_features(_SLOPE_BAND_REQUEST) == ["slope_band"]
    assert (
        sim_batch._request_features({"measurements": [{"name": "x", "spice": "."}]})
        == []
    )
    assert "slope_band" in sim_batch._MANDATORY_RUNNER_FEATURES
    assert sim_batch._missing_runner_features(["slope_band"], "0.9.1") == ["slope_band"]
    assert sim_batch._missing_runner_features(["slope_band"], "0.9.2") == []
    assert sim_batch._missing_runner_features(["slope_band"], None) is None
