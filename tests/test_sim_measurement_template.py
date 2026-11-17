"""Unit tests for `klt sim`'s measurement templates over a case list (issue #5731).

Expansion and refusal behavior of `options.measurement_cases`: declare the N
copy scopes once, write each reduction once with a `{case}` placeholder, and
`klt sim` expands the entry into one ordinary measurement per case before any
corner is dispatched. These tests need no simulator -- every malformed
combination is refused before an engine could launch.

The run-level behavior (grading, identity of an untemplated request, batch
forwarding, and the real-ngspice replicated-deck integration) lives in
`tests/test_sim_measurement_template_run.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_measurement_template as mt

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


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


# With more than one case, every substitutable field the per-case scope has to
# reach must carry the placeholder: one missing from `name` expands N entries
# to one shared name; one missing from the card text emits N identical cards.


def test_case_placeholder_only_in_the_spice_card_names_the_name_field():
    request = {
        "options": {"measurement_cases": ["case0_", "case1_", "case2_"]},
        "measurements": [
            {"name": "tpd", "spice": ".meas tran tpd FIND v({case}out) AT=1u"}
        ],
    }
    _assert_refused(
        request,
        mt.MISSING_SUBSTITUTION_CODE,
        r"only in measurements\[\]\.spice.*share the one name 'tpd'",
    )


def test_case_placeholder_only_in_the_expr_names_the_name_field():
    request = {
        "options": {"measurement_cases": ["c0_", "c1_"]},
        "measurements": [{"name": "tpd", "expr": "v({case}out)"}],
    }
    _assert_refused(request, mt.MISSING_SUBSTITUTION_CODE, r"measurements\[\]\.name")


def test_case_placeholder_only_in_the_name_leaves_the_card_static():
    request = {
        "options": {"measurement_cases": ["case0_", "case1_", "case2_"]},
        "measurements": [
            {"name": "tpd_{case}", "spice": ".meas tran tpd FIND v(out) AT=1u"}
        ],
    }
    _assert_refused(request, mt.MISSING_SUBSTITUTION_CODE, r"identical spice card")


def test_a_single_case_allows_the_placeholder_in_the_card_only():
    request = {
        "options": {"measurement_cases": ["c0_"]},
        "measurements": [
            {"name": "tpd", "spice": ".meas tran tpd FIND v({case}out) AT=1u"}
        ],
    }
    out = mt.expand_request(request)
    (spec,) = out["measurements"]
    assert spec["name"] == "tpd"
    assert spec["spice"] == ".meas tran tpd FIND v(c0_out) AT=1u"


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
