"""Tests for ``measurements[].snm`` -- butterfly static noise margin graded
through ``klt sim`` (issue #3895).

Most tests stub ``ngspice -b`` and feed a hand-built rawfile (cf.
tests/test_sim_transfer_meas.py); the ``real_ngspice`` test runs the engine on
a pair of behavioural tanh inverters whose margin is known by direct search.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.snm_oracle import brute_force_eye, tanh_inverter
from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_batch, sim_snm_meas

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_BODY = {"x": "v(in)", "y1": "v(out_a)", "y2": "v(out_b)"}
_DECL = {"name": "snm_hold", "snm": _BODY}

_XS = [i / 400 for i in range(401)]
_YS = [tanh_inverter(x) for x in _XS]
_EXPECTED = brute_force_eye()


def _write_body(tmp_path: Path) -> None:
    (tmp_path / "body.spice").write_text(
        "Vin in 0 DC 0\n"
        "Ba out_a 0 V = 0.5*(1-tanh(5*(V(in)-0.5)))\n"
        "Bb out_b 0 V = 0.5*(1-tanh(5*(V(in)-0.5)))\n"
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


def _rawfile(xs, ys1, ys2, *, nvars: int = 4, points: int | None = None) -> str:
    n = len(xs) if points is None else points
    names = ["v-sweep", "v(__klt_snm_0_x)", "v(__klt_snm_0_y1)", "v(__klt_snm_0_y2)"]
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
    for i, (x, a, b) in enumerate(zip(xs, ys1, ys2, strict=True)):
        lines += [f" {i}\t{x!r}", f"\t{x!r}", f"\t{a!r}", f"\t{b!r}"][: nvars + 1]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not-an-object", "must be an object"),
        ({**_BODY, "extra": 1}, "unknown snm key"),
        ({"y1": "v(a)", "y2": "v(b)"}, "snm.x"),
        ({**_BODY, "y1": 5}, "snm.y1"),
        ({**_BODY, "y2": "v(a b)"}, "snm.y2"),
        ({**_BODY, "x": "i(v1)"}, "snm.x"),
        ({**_BODY, "y1": "v()"}, "snm.y1"),
    ],
)
def test_malformed_snm_is_rejected(body, message):
    with pytest.raises(sim.SimError, match=message):
        sim._validate_measurement_forms([{"name": "s", "snm": body}])


@pytest.mark.parametrize(
    "other",
    ["spice", "expr", "crossings", "cycle_mean", "energy", "noise", "transfer", "tian"],
)
def test_conflicting_forms_are_rejected(other):
    spec = {"name": "s", "snm": _BODY, other: {"x": 1} if other[0] in "cent" else "x"}
    with pytest.raises(sim.SimError, match=f"'snm' together with '{other}'"):
        sim._validate_measurement_forms([spec])


def test_valid_snm_is_accepted_and_selectors_normalise():
    sim._validate_measurement_forms([_DECL])
    assert sim_snm_meas.normalize_selector("V(OUT_A)") == "v(out_a)"
    assert sim_snm_meas.normalize_selector(" v(a)") is None


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


def test_duplicate_names_are_rejected(tmp_path):
    other = {"name": "SNM_HOLD", "snm": {**_BODY, "y2": "v(c)"}}
    request = _request(tmp_path, [_DECL, other])
    with pytest.raises(sim.SimError, match="duplicate measurement name"):
        sim.run_sim(str(request))


def test_refused_for_xyce(tmp_path):
    request = _request(tmp_path, [_DECL], engine="xyce")
    with pytest.raises(sim.SimError, match="snm is not supported for engine"):
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


def test_deck_dumps_each_unique_triple_once_after_the_analysis(tmp_path):
    specs = [
        _DECL,
        {"name": "again", "snm": {"x": "V(IN)", "y1": "v(out_a)", "y2": "v(out_b)"}},
        {"name": "other", "snm": {**_BODY, "y2": "v(out_c)"}},
    ]
    lines = _deck(tmp_path, specs)
    dc = lines.index("dc Vin 0 1 0.01")
    assert lines.index("set filetype=ascii") < dc
    assert lines.count("let __klt_snm_0_x = v(in)") == 1
    assert "let __klt_snm_1_y2 = v(out_c)" in lines
    write = lines.index(
        f"write {tmp_path / 'snm_0.raw'} __klt_snm_0_x __klt_snm_0_y1 __klt_snm_0_y2"
    )
    assert dc < lines.index("let __klt_snm_0_x = v(in)") < write < lines.index("quit")
    assert not any("__klt_snm_2" in line for line in lines)


def test_deck_without_snm_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas dc v MAX v(out_a)"}])
    assert not any("__klt_snm" in line or "filetype" in line for line in lines)


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading, error tokens, response shape
# --------------------------------------------------------------------------- #


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
            with open(os.path.join(cwd, "snm_0.raw"), "w", encoding="utf-8") as h:
                h.write(raw)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


def _only_corner(report):
    (corner,) = report["corners"]
    return corner, {m["name"]: m for m in corner["measurements"]}


def test_value_is_graded_and_carries_the_eye_breakdown(tmp_path, monkeypatch):
    specs = [
        {**_DECL, "unit": "V", "limits": {"min": 0.1}},
        {"name": "tight", "snm": _BODY, "limits": {"min": 0.9}},
    ]
    request = _request(tmp_path, specs[:1])
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS, _YS))

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    entry = by_name["snm_hold"]
    assert entry["value"] == pytest.approx(_EXPECTED, abs=2e-3)
    assert entry["status"] == "pass"
    assert entry["margin"] == pytest.approx(entry["value"] - 0.1)
    assert entry["snm"]["x"] == "v(in)"
    assert entry["snm"]["eye_upper_left"] == pytest.approx(
        entry["snm"]["eye_lower_right"], rel=1e-6
    )
    assert corner["diagnostics"] == []

    failing = _request(tmp_path, specs[1:])
    report = sim.run_sim(str(failing))
    _, by_name = _only_corner(report)
    assert by_name["tight"]["status"] == "fail"
    assert report["status"] == "fail"


def test_monte_carlo_statistics_carry_the_value(tmp_path, monkeypatch):
    request = _request(
        tmp_path,
        [{**_DECL, "limits": {"min": 0.1}}],
        monte_carlo={"n": 3, "seed": 5, "vary": "mismatch"},
    )
    _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS, _YS))

    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 3
    values = [c["measurements"][0]["value"] for c in report["corners"]]
    assert all(v == pytest.approx(_EXPECTED, abs=2e-3) for v in values)
    (rollup,) = report["measurements"]
    assert rollup["status"] == "pass"
    assert rollup["monte_carlo"]["n"] == 3
    assert rollup["monte_carlo"]["mean"] == pytest.approx(_EXPECTED, abs=2e-3)


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        (None, "[snm_vector_missing]"),
        ("garbage\n", "[snm_raw_invalid]"),
        (_rawfile(_XS, _YS, _YS, nvars=3), "[snm_raw_invalid]"),
        (_rawfile(_XS[:2], _YS[:2], _YS[:2]), "[snm_invalid_data]"),
        (_rawfile(_XS, _YS[::-1], _YS), "[snm_non_monotonic]"),
        (
            _rawfile(_XS, [1 - x for x in _XS], [1 - x for x in _XS]),
            "[snm_no_second_eye]",
        ),
    ],
)
def test_failures_are_errors_with_a_reason_token_never_a_pass(
    tmp_path, monkeypatch, raw, token
):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 0.0}}])
    _stub_ngspice(monkeypatch, raw=raw)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    entry = by_name["snm_hold"]
    assert entry["status"] == "error"
    assert entry["value"] is None
    (diag,) = corner["diagnostics"]
    assert diag["code"] == "measurement"
    assert diag["message"].startswith(token)
    assert report["status"] != "pass"


def test_unique_triple_is_computed_once(tmp_path, monkeypatch):
    specs = [_DECL, {"name": "again", "snm": _BODY, "limits": {"min": 0.1}}]
    request = _request(tmp_path, specs)
    decks = _stub_ngspice(monkeypatch, raw=_rawfile(_XS, _YS, _YS))
    calls = []
    real = sim_snm_meas.sim_snm.analyze_snm
    monkeypatch.setattr(
        sim_snm_meas.sim_snm, "analyze_snm", lambda *a: calls.append(1) or real(*a)
    )

    report = sim.run_sim(str(request))

    _, by_name = _only_corner(report)
    assert by_name["snm_hold"]["value"] == by_name["again"]["value"]
    assert len(calls) == 1
    assert decks[0].count("write ") == 1


# --------------------------------------------------------------------------- #
# Real ngspice
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_real_ngspice_behavioural_inverters_match_the_geometric_search(tmp_path):
    specs = [
        {**_DECL, "limits": {"min": 0.1, "max": 1.0}},
        {"name": "bad", "snm": {**_BODY, "y2": "v(nope)"}},
    ]
    request = _request(tmp_path, specs)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    # 400 sweep steps: polyline error is far below the 5e-3 tolerance.
    assert by_name["snm_hold"]["value"] == pytest.approx(_EXPECTED, abs=5e-3)
    assert by_name["snm_hold"]["status"] == "pass"
    assert by_name["bad"]["status"] == "error"
    assert [d["message"][:20] for d in corner["diagnostics"]] == [
        "[snm_vector_missing]"
    ]


# --------------------------------------------------------------------------- #
# Mandatory batch runner feature
# --------------------------------------------------------------------------- #

_SNM_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [_DECL],
}


def test_snm_is_a_registered_mandatory_runner_feature():
    assert sim_batch._request_features(_SNM_REQUEST) == ["snm"]
    assert (
        sim_batch._request_features({"measurements": [{"name": "x", "spice": "."}]})
        == []
    )
    assert "snm" in sim_batch._MANDATORY_RUNNER_FEATURES
    assert sim_batch._missing_runner_features(["snm"], "0.7.0") == ["snm"]
    assert sim_batch._missing_runner_features(["snm"], "0.7.1") == []
    assert sim_batch._missing_runner_features(["snm"], None) is None
