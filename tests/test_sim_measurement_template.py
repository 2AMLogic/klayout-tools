"""Tests for `klt sim`'s measurement templates over a declared case list (issue #5731).

A replicated-deck transient grid -- N independent copies of one circuit per
deck, each with its own node/source names, `.ic` cards and stimulus -- had no
request shape that scaled: `corners.params` used as a case axis costs one
ngspice process per case per PVT point, and hand-declaring every card against
every copy's names costs N x M declarations. `options.measurement_cases`
closes it: declare the N scopes once, write each reduction once with a
`{case}` placeholder, and `klt sim` expands the entry into one ordinary
measurement per case before any corner is dispatched, each result carrying a
`case` provenance field.

Unit tests need no simulator; integration tests run the real `ngspice -b`
and skip without it. The batch-backend test reuses the fake transport helpers
from `tests/test_sim_batch.py` (no fleet submit), following the issue's test
plan.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import test_sim_batch as tb
from klayout_tools import sim
from klayout_tools import sim_measurement_template as mt

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# Expansion: templated x {1, 2, N} cases
# --------------------------------------------------------------------------- #


def _templated_request(cases: list[str]) -> dict:
    return {
        "analysis": {"kind": "tran", "args": "10n 2u"},
        "options": {"measurement_cases": cases},
        "measurements": [
            {
                "name": "tpd_{case}",
                "spice": ".meas tran tpd_{case} TRIG v({case}in) VAL=0.5 "
                "RISE=1 TARG v({case}out) VAL=0.5 RISE=1",
            },
            {"name": "flat", "spice": ".meas tran flat AVG v(vdd) FROM=1n TO=2u"},
        ],
    }


@pytest.mark.parametrize(
    ("cases", "expected"),
    [
        (["c_"], ["tpd_c_"]),
        (["c0_", "c1_"], ["tpd_c0_", "tpd_c1_"]),
        (
            ["case0_", "case1_", "case2_", "case3_"],
            ["tpd_case0_", "tpd_case1_", "tpd_case2_", "tpd_case3_"],
        ),
    ],
)
def test_one_templated_entry_expands_to_one_measurement_per_case(cases, expected):
    out = mt.expand_request(_templated_request(cases))
    names = [spec["name"] for spec in out["measurements"]]
    assert names == expected + ["flat"]  # the untemplated entry stays in place


def test_every_occurrence_of_the_placeholder_is_substituted():
    out = mt.expand_request(_templated_request(["c0_"]))
    (spec,) = [s for s in out["measurements"] if s.get("case")]
    assert spec["spice"] == (
        ".meas tran tpd_c0_ TRIG v(c0_in) VAL=0.5 RISE=1 TARG v(c0_out) VAL=0.5 RISE=1"
    )


def test_expanded_specs_carry_the_case_provenance_field_in_declared_order():
    out = mt.expand_request(_templated_request(["case0_", "case1_", "case2_"]))
    cases = [spec.get("case") for spec in out["measurements"]]
    assert cases == ["case0_", "case1_", "case2_", None]


def test_expansion_keeps_every_other_field_of_the_entry():
    request = _templated_request(["c0_"])
    request["measurements"][0]["unit"] = "s"
    request["measurements"][0]["limits"] = {"max": 200e-12}
    out = mt.expand_request(request)
    for spec in out["measurements"][:1]:
        assert spec["unit"] == "s"
        assert spec["limits"] == {"max": 200e-12}


def test_a_prefix_scope_and_its_extension_stay_distinct():
    out = mt.expand_request(_templated_request(["case1_", "case11_"]))
    assert [s["name"] for s in out["measurements"]] == [
        "tpd_case1_",
        "tpd_case11_",
        "flat",
    ]
    assert out["measurements"][0]["spice"] != out["measurements"][1]["spice"]


def test_templated_expr_entry_referencing_another_templated_measurement():
    request = _templated_request(["c0_", "c1_"])
    request["measurements"].insert(
        0,
        {"name": "x_{case}", "expr": "v({case}out)"},
    )
    out = mt.expand_request(request)
    assert [s["name"] for s in out["measurements"]] == [
        "x_c0_",
        "x_c1_",
        "tpd_c0_",
        "tpd_c1_",
        "flat",
    ]
    assert out["measurements"][0]["expr"] == "v(c0_out)"
    assert out["measurements"][1]["expr"] == "v(c1_out)"


def test_request_declaring_neither_is_returned_unchanged_identity():
    request = {
        "analysis": {"kind": "tran", "args": "1n"},
        "measurements": [{"name": "a", "spice": ".meas tran a FIND v(out) AT=1u"}],
    }
    assert mt.expand_request(request) is request


def test_request_with_the_option_absent_and_no_placeholder_keeps_options_object():
    request = {
        "analysis": {"kind": "tran", "args": "1n"},
        "measurements": [{"name": "a", "spice": ".meas tran a FIND v(out) AT=1u"}],
    }
    assert mt.expand_request(request) is request


def test_expanded_request_drops_the_option_key_from_the_forwarded_document():
    out = mt.expand_request(_templated_request(["c0_", "c1_"]))
    assert "measurement_cases" not in out["options"]


# --------------------------------------------------------------------------- #
# Refusals: message and error code, before anything runs
# --------------------------------------------------------------------------- #


def _assert_refused(request: dict, code: str, match: str) -> None:
    with pytest.raises(sim.SimError, match=match) as excinfo:
        mt.expand_request(request)
    assert excinfo.value.code == code


def test_placeholder_without_a_case_list_names_the_field():
    request = {
        "measurements": [
            {"name": "x_{case}", "spice": ".meas tran x FIND v(out) AT=1u"}
        ]
    }
    _assert_refused(request, mt.TEMPLATE_WITHOUT_CASES_CODE, r"measurements\[\]\.name")


def test_placeholder_in_an_unsupported_field_names_the_field():
    request = {
        "options": {"measurement_cases": ["c0_", "c1_"]},
        "measurements": [
            {
                "name": "x_{case}",
                "spice": ".meas tran x FIND v(out) AT=1u",
                "unit": "{case}_V",
            }
        ],
    }
    _assert_refused(request, mt.FIELD_UNSUPPORTED_CODE, r"measurements\[\]\.unit")


def test_placeholder_nested_in_a_form_object_names_the_top_level_field():
    request = {
        "options": {"measurement_cases": ["c0_"]},
        "measurements": [
            {"name": "x", "crossings": {"signal": "{case}out", "level": 0.5}}
        ],
    }
    _assert_refused(request, mt.FIELD_UNSUPPORTED_CODE, r"measurements\[\]\.crossings")


@pytest.mark.parametrize(
    "cases",
    [
        [],
        "c0_",
        [""],
        [7],
        ["c 0"],
        ["c.0"],
        ["c0_", "c0_"],
    ],
)
def test_malformed_case_lists_are_refused_with_the_list_code(cases):
    request = (
        _templated_request(cases)
        if isinstance(cases, list) and cases
        else {
            "options": {"measurement_cases": cases},
            "measurements": [
                {"name": "x_{case}", "spice": ".meas tran x FIND v(out) AT=1u"}
            ],
        }
    )
    _assert_refused(request, mt.CASES_INVALID_CODE, r"options\.measurement_cases")


def test_case_list_without_a_templated_entry_is_refused():
    request = {
        "options": {"measurement_cases": ["c0_", "c1_"]},
        "measurements": [
            {"name": "flat", "spice": ".meas tran flat FIND v(out) AT=1u"}
        ],
    }
    _assert_refused(
        request, mt.CASES_WITHOUT_TEMPLATE_CODE, r"no\s+measurements\[\] entry"
    )


def test_case_list_with_no_measurements_at_all_is_refused():
    request = {"options": {"measurement_cases": ["c0_"]}}
    _assert_refused(
        request, mt.CASES_WITHOUT_TEMPLATE_CODE, r"options\.measurement_cases"
    )


def test_templated_entries_with_analysis_steps_are_refused():
    request = {
        "analysis_steps": [
            {"name": "s1", "analysis": {"kind": "tran", "args": "1n 10n"}}
        ],
        "options": {"measurement_cases": ["c0_", "c1_"]},
        "measurements": [
            {"name": "x_{case}", "spice": ".meas tran x FIND v(out) AT=1u"}
        ],
    }
    _assert_refused(request, mt.STEPS_COMBINATION_CODE, r"analysis_steps")


def test_refusals_fire_before_any_engine_launch(tmp_path, monkeypatch):
    _body = tmp_path / "body.spice"
    _body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")

    def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("engine launched")

    monkeypatch.setattr(sim.subprocess, "run", boom)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "options": {"measurement_cases": []},
            "measurements": [
                {"name": "x_{case}", "spice": ".meas tran x FIND v(out) AT=1u"}
            ],
        },
    )
    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(request))
    assert excinfo.value.code == mt.CASES_INVALID_CODE


# --------------------------------------------------------------------------- #
# Expanded names go through the existing validation
# --------------------------------------------------------------------------- #


def test_case_differing_scopes_collide_on_the_expanded_names(tmp_path):
    _body = tmp_path / "body.spice"
    _body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "options": {"measurement_cases": ["case0_", "Case0_"]},
            "measurements": [
                {
                    "name": "tpd_{case}",
                    "spice": ".meas tran tpd_{case} FIND v({case}out) AT=1u",
                }
            ],
        },
    )
    with pytest.raises(sim.SimError, match="'tpd_case0_' and 'tpd_Case0_'"):
        sim.run_sim(str(request))


def test_intermediate_templated_entry_hits_the_existing_scalar_refusal(tmp_path):
    _body = tmp_path / "body.spice"
    _body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "options": {"measurement_cases": ["c0_"]},
            "measurements": [
                {"name": "x_{case}", "expr": "v({case}out)", "intermediate": True}
            ],
        },
    )
    with pytest.raises(sim.SimError, match="intermediate.*analysis_steps|'x_c0_'"):
        sim.run_sim(str(request))


def test_two_templates_expanding_to_one_name_are_refused(tmp_path):
    _body = tmp_path / "body.spice"
    _body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "options": {"measurement_cases": ["a", "x"]},
            "measurements": [
                {"name": "{case}x", "expr": "v(out)"},
                {"name": "a{case}", "expr": "v(out)"},
            ],
        },
    )
    with pytest.raises(sim.SimError, match="'ax'"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Grading: limits, plausible_range, Monte Carlo (engine-free stubs)
# --------------------------------------------------------------------------- #


def _stub_log_values(monkeypatch, values_per_call: dict[str, float]) -> None:
    """Stub ngspice so each corner run's log reports every expanded name."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            for name, value in values_per_call.items():
                handle.write(f"{name} = {value!r}\n")
        return tb.fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _replicated_stub_request(tmp_path: Path, **overrides) -> Path:
    body = tmp_path / "body.spice"
    body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request: dict[str, object] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 10n"},
        "options": {"measurement_cases": ["case0_", "case1_"]},
        "measurements": [
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
            }
        ],
    }
    request.update(overrides)
    return _write_request(tmp_path, request)


def test_limits_grade_expanded_results_independently(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        measurements=[
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
                "limits": {"min": 0.0, "max": 0.5},
            }
        ],
    )
    _stub_log_values(monkeypatch, {"tp_case0_": 0.4, "tp_case1_": 0.9})

    report = sim.run_sim(str(request))

    assert [e["name"] for e in report["measurements"]] == ["tp_case0_", "tp_case1_"]
    assert [e["status"] for e in report["measurements"]] == ["pass", "fail"]
    assert [e["case"] for e in report["measurements"]] == ["case0_", "case1_"]


def test_plausible_range_grades_expanded_results(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        measurements=[
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
                "plausible_range": {"min": 0.0, "max": 0.5},
            }
        ],
    )
    _stub_log_values(monkeypatch, {"tp_case0_": 0.4, "tp_case1_": 0.9})

    report = sim.run_sim(str(request))

    implausible = [
        m for m in report["corners"][0]["measurements"] if m["status"] == "inconclusive"
    ]
    assert [m["name"] for m in implausible] == ["tp_case1_"]
    # Pre-existing corner semantics: one implausible value makes the whole
    # corner (hence both rollup entries) inconclusive.
    assert [
        e["name"] for e in report["measurements"] if e["status"] == "inconclusive"
    ] == [
        "tp_case0_",
        "tp_case1_",
    ]


def test_monte_carlo_rollup_covers_every_expanded_name(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        monte_carlo={"n": 5, "seed": 1, "vary": "mismatch"},
    )
    samples = [3.0, 1.0, 5.0, 2.0, 4.0]
    state = {"index": 0}

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = cmd[cmd.index("-o") + 1]
        value = samples[state["index"]]
        state["index"] += 1
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(f"tp_case0_ = {value!r}\n")
            handle.write(f"tp_case1_ = {value * 2!r}\n")
        return tb.fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 5  # one process per sample, N cases each
    by_name = {e["name"]: e for e in report["measurements"]}
    assert by_name["tp_case0_"]["monte_carlo"]["mean"] == pytest.approx(3.0)
    assert by_name["tp_case1_"]["monte_carlo"]["mean"] == pytest.approx(6.0)
    assert by_name["tp_case0_"]["case"] == "case0_"
    assert by_name["tp_case1_"]["case"] == "case1_"


# --------------------------------------------------------------------------- #
# Identity: a request declaring neither changes nothing
# --------------------------------------------------------------------------- #


def test_untemplated_request_keeps_report_entries_free_of_the_case_field(
    tmp_path, monkeypatch
):
    body = tmp_path / "body.spice"
    body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "measurements": [{"name": "a", "spice": ".meas tran a FIND v(out) AT=1u"}],
        },
    )
    _stub_log_values(monkeypatch, {"a": 1.0})

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    assert "case" not in entry
    (corner,) = report["corners"]
    assert "case" not in corner["measurements"][0]


# --------------------------------------------------------------------------- #
# Batch backend: the forwarded document is already expanded (fake runner,
# no fleet submit)
# --------------------------------------------------------------------------- #


def test_batch_forwarded_document_carries_expanded_measurements(tmp_path, monkeypatch):
    runner = tb._FakeRunner()
    runner.default = tb.fake_completed(tb._status("done"))
    runner.outputs = {"report.json": tb._sim_report()}
    tb._install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded = tb._capture_uploads(monkeypatch, runner)
    body = tmp_path / "body.spice"
    body.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    request = tb._batch_request(tmp_path)
    request["options"] = {"measurement_cases": ["c0_", "c1_"]}
    request["measurements"] = [
        {
            "name": "vdd_{case}",
            "spice": ".meas tran vdd_{case} AVG v({case}out) FROM=1n TO=900n",
        }
    ]

    sim.run_sim(str(_write_request(tmp_path, request)))

    forwarded = json.loads(uploaded["request.json"])
    assert [m["name"] for m in forwarded["measurements"]] == ["vdd_c0_", "vdd_c1_"]
    assert [m.get("case") for m in forwarded["measurements"]] == ["c0_", "c1_"]
    assert "measurement_cases" not in forwarded.get("options", {})


# --------------------------------------------------------------------------- #
# Integration: replicated deck, templated == hand-declared (real ngspice)
# --------------------------------------------------------------------------- #


_CASES = ["case0_", "case1_", "case2_"]

_REPLICATED_BODY = """\
* replicated 3-case RC bench: independent copies sharing only ideal sources
Vcase0_drive case0_in 0 PWL(0 0 100p 1)
Rcase0_r case0_in case0_out 1k
Ccase0_c case0_out 0 1p
.ic v(case0_out)=0.2
Vcase1_drive case1_in 0 PWL(0 0 100p 1)
Rcase1_r case1_in case1_out 1k
Ccase1_c case1_out 0 1p
.ic v(case1_out)=0.2
Vcase2_drive case2_in 0 PWL(0 0 100p 1)
Rcase2_r case2_in case2_out 1k
Ccase2_c case2_out 0 1p
.ic v(case2_out)=0.2
"""

_TEMPLATED_MEASUREMENTS = [
    {
        "name": "vstart_{case}",
        "spice": ".meas tran vstart_{case} FIND v({case}out) AT=2p",
        "unit": "V",
    },
    {
        "name": "vfinal_{case}",
        "spice": ".meas tran vfinal_{case} FIND v({case}out) AT=5n",
        "unit": "V",
    },
]


def _hand_declared_measurements() -> list[dict]:
    out = []
    for spec in _TEMPLATED_MEASUREMENTS:
        for case in _CASES:
            out.append(
                {
                    "name": spec["name"].replace("{case}", case),
                    "spice": spec["spice"].replace("{case}", case),
                    "unit": spec["unit"],
                }
            )
    return out


def _replicated_request(
    tmp_path: Path, measurements: list[dict], cases=None, keep_artifacts=False
) -> Path:
    body = tmp_path / "body.spice"
    body.write_text(_REPLICATED_BODY)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1p 5n 0 5p"},
        "measurements": measurements,
    }
    options: dict[str, object] = {}
    if cases is not None:
        options["measurement_cases"] = cases
    if keep_artifacts:
        options["keep_artifacts"] = True
    if options:
        request["options"] = options
    return _write_request(tmp_path, request)


@_SKIP_NO_NGSPICE
def test_templated_replicated_deck_matches_the_hand_declared_grid(tmp_path):
    template_cases = [c for c in _CASES]
    templated = sim.run_sim(
        str(_replicated_request(tmp_path, _TEMPLATED_MEASUREMENTS, template_cases))
    )
    hand = sim.run_sim(
        str(_replicated_request(tmp_path, _hand_declared_measurements()))
    )

    def values(report):
        corner = report["corners"][0]
        return {m["name"]: m["value"] for m in corner["measurements"]}

    assert values(templated) == values(hand)
    # Every expanded result and rollup entry carries its `case` (spec-major
    # expansion order: each templated entry expands across all its cases).
    corner = templated["corners"][0]
    assert [m["case"] for m in corner["measurements"]] == [
        c for _ in range(2) for c in _CASES
    ]
    assert [e["case"] for e in templated["measurements"]] == [
        c for _ in range(2) for c in _CASES
    ]
    # The declared-once `limits` grade per expanded case.
    assert all(e["status"] == "pass" for e in templated["measurements"])


@_SKIP_NO_NGSPICE
def test_replicated_deck_body_level_ic_constrains_each_copy(tmp_path):
    """Each copy's own body-level `.ic` sets its start state (no `uic`):
    `vstart_<case>` reads the 0.2 V the card declares, while the solve runs
    the ordinary OP for everything else -- see the docs' measured note."""
    request = _replicated_request(
        tmp_path,
        [
            {
                "name": "vstart_{case}",
                "spice": ".meas tran vstart_{case} FIND v({case}out) AT=2p",
                "unit": "V",
            }
        ],
        _CASES,
    )
    report = sim.run_sim(str(request))
    for entry in report["corners"][0]["measurements"]:
        assert entry["value"] == pytest.approx(0.2, abs=1e-3)


@_SKIP_NO_NGSPICE
def test_templated_and_hand_declared_decks_emit_the_same_cards(tmp_path):
    reports = []
    for make_request in (
        lambda: _replicated_request(
            tmp_path, _TEMPLATED_MEASUREMENTS, _CASES, keep_artifacts=True
        ),
        lambda: _replicated_request(
            tmp_path, _hand_declared_measurements(), keep_artifacts=True
        ),
    ):
        report = sim.run_sim(str(make_request()))
        deck_path = report["corners"][0]["artifacts"]["deck"]
        assert deck_path and Path(deck_path).exists()
        reports.append(deck_path)
    cards = [
        [
            line
            for line in Path(p).read_text().splitlines()
            if line.startswith(".meas") or line.startswith(".ic")
        ]
        for p in reports
    ]
    assert cards[0] == cards[1]
    assert "{case}" not in Path(reports[0]).read_text()
