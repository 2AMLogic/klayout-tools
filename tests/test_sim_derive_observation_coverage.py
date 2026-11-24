"""Coverage for characterization-only derived observations on a mixed grid."""

import pytest

from klayout_tools.coverage import work_id
from test_sim_derive_run import DERIVE, GROUPS, SOURCE, _run

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_only_the_group_that_produced_a_value_has_a_checked_observation(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setitem(GROUPS, ("a", 1.0), [1.0, 1.0, 1.0])
    source = {**SOURCE, "plausible_range": {"max": 1.5}}
    derived = {"name": "span", "derive": DERIVE["derive"]}
    code, report = _run(tmp_path, monkeypatch, capsys, [source, derived])

    groups = report["measurements"][1]["derive"]["groups"]
    assert code == 4 and report["status"] == "inconclusive"
    assert [(g["status"], g["value"], g["reason"]) for g in groups] == [
        ("pass", 0.0, None),
        ("inconclusive", None, "source_inconclusive"),
    ]
    measured, missing = [
        work_id("derived_measurement", "span", group["group_id"]) + "/observation"
        for group in groups
    ]
    coverage = report["coverage"]
    assert measured in coverage["checked"]
    assert missing not in coverage["checked"]
    assert {"id": missing, "reason": "source_inconclusive"} in coverage["skipped"]


def test_finite_implausible_derived_values_still_count_as_observations(
    tmp_path, monkeypatch, capsys
):
    derived = {
        "name": "span",
        "derive": DERIVE["derive"],
        "plausible_range": {"max": 0.1},
    }
    code, report = _run(tmp_path, monkeypatch, capsys, [SOURCE, derived])

    assert code == 4 and report["status"] == "inconclusive"
    for group in report["measurements"][1]["derive"]["groups"]:
        assert group["reason"] == "implausible_value" and group["value"] > 0.1
        observation = work_id("derived_measurement", "span", group["group_id"])
        assert observation + "/observation" in report["coverage"]["checked"]
