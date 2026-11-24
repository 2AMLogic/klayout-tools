"""Issue #7807: the ``requires`` assertion on an item-5 evidence member.

``content_hash`` pins a citation's request netlist, and that artifact is
byte-identical across a reduced-coverage probe and the complete matrix minted
from the same request -- so both graded ``met`` and either could be cited in
the other's place. A member may now carry ``requires`` (``grid`` and/or
``ngspice_options``) and the grader checks it against the cited envelope's
own ``corners[]`` / ``environment.ngspice_options``. These cover the
reproduction, the shortfall detail, the solver-profile subset check, the
malformed/unsupported refusals and the no-``requires`` non-regression.
Fixtures are the hand-built envelopes ``joint_grid`` already uses
(``helpers/signoff_joint_fixtures.py``).
"""

from __future__ import annotations

import pytest

from helpers.signoff_joint_fixtures import _GRID, _grid_envelope, _item5, _write
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_joint_rows import member_shortfall, missing_grid_points
from klayout_tools.signoff_member_requires import read_requires

OPTIONS = {"reltol": 0.001, "abstol": 1e-12}


def _reduced_envelope(count: int = 2) -> dict:
    """A reduced-coverage run: the first ``count`` corners of the full grid,
    same request skeleton (so the same ``provenance.input.content_hash``)."""
    envelope = _grid_envelope()
    envelope["corners"] = envelope["corners"][:count]
    envelope["corner_count"] = envelope["passed"] = count
    return envelope


def _with_options(envelope: dict, options) -> dict:
    environment = {} if options is None else {"ngspice_options": options}
    return {**envelope, "environment": environment}


def _graded(tmp_path, envelope, requires, name="a.json"):
    path = _write(tmp_path, name, envelope)
    item = _item5({"5": [{"file": path, "requires": requires}]})
    return path, item


# --------------------------------------------------------------------------- #
# The reproduction: identical request netlist, different coverage.
# --------------------------------------------------------------------------- #


def test_probe_and_full_matrix_share_a_hash_but_only_the_full_one_is_met(tmp_path):
    probe, full = _reduced_envelope(), _grid_envelope()
    # The premise of the issue: the pinned artifact cannot tell them apart.
    assert probe["provenance"]["input"] == full["provenance"]["input"]
    requires = {"grid": _GRID}

    # Without `requires` the probe is a perfectly good citation (the gap).
    unbound = _item5({"5": [{"file": _write(tmp_path, "probe.json", probe)}]})
    assert unbound["status"] == "met"

    _, probe_item = _graded(tmp_path, probe, requires, "probe.json")
    _, full_item = _graded(tmp_path, full, requires, "full.json")

    assert full_item["status"] == "met"
    assert probe_item["status"] == "unmet"
    assert probe_item["reason"] == "partial_coverage"
    (entry,) = probe_item["detail"]["requires_unmet"]
    assert len(entry["missing_points"]) == 25  # 27 required - 2 present
    assert "tt/1.620V/-40C" not in entry["missing_points"]  # present, not listed
    assert "ff/1.980V/125C" in entry["missing_points"]


def test_a_lone_citation_object_carries_requires_too(tmp_path):
    path = _write(tmp_path, "probe.json", _reduced_envelope())

    item = _item5({"5": {"file": path, "requires": {"grid": _GRID}}})

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["requires_unmet"][0]["member"] == path


def test_a_missing_point_is_named_in_the_envelope_corner_id_spelling(tmp_path):
    path, item = _graded(
        tmp_path, _grid_envelope(drop=("ff", 1.98, -40)), {"grid": _GRID}
    )

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["requires_unmet"] == [
        {"member": path, "missing_points": ["ff/1.980V/-40C"]}
    ]


def test_a_failing_corner_at_a_required_point_is_a_missing_point(tmp_path):
    _, item = _graded(tmp_path, _grid_envelope(fail=("tt", 1.62, 27)), {"grid": _GRID})

    assert item["detail"]["requires_unmet"][0]["missing_points"] == ["tt/1.620V/27C"]


def test_a_point_swept_twice_must_pass_at_every_corner(tmp_path):
    envelope = _grid_envelope()
    twin = dict(envelope["corners"][0], status="fail")  # same point, second sweep
    envelope["corners"].append(twin)

    _, item = _graded(tmp_path, envelope, {"grid": _GRID})

    assert item["detail"]["requires_unmet"][0]["missing_points"] == ["tt/1.620V/-40C"]


def test_integral_and_float_axis_values_are_one_point(tmp_path):
    grid = {**_GRID, "temperature_c": [-40.0, 27.0, 125.0]}  # envelope has ints

    _, item = _graded(tmp_path, _grid_envelope(), {"grid": grid})

    assert item["status"] == "met"


def test_a_partial_grid_declares_only_the_axes_it_constrains(tmp_path):
    # supply + temperature only: process takes the request-side default (no
    # process axis), so a process-tagged envelope does not sit on those points.
    requires = {"grid": {"temperature_c": [27]}}
    _, item = _graded(tmp_path, _grid_envelope(), requires)

    assert item["status"] == "unmet"
    assert item["detail"]["requires_unmet"][0]["missing_points"] == [
        "default/novdd/27C"
    ]


# --------------------------------------------------------------------------- #
# requires.ngspice_options -- a subset check against the echoed profile.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "required",
    [{"reltol": 0.001}, {"reltol": 1e-3, "abstol": 1e-12}, {"abstol": 1e-12}],
)
def test_a_matching_solver_profile_is_met(tmp_path, required):
    envelope = _with_options(_grid_envelope(), OPTIONS)

    _, item = _graded(tmp_path, envelope, {"ngspice_options": required})

    assert item["status"] == "met"


def test_a_tighter_tolerance_companion_is_refused_with_observed_values(tmp_path):
    envelope = _with_options(_grid_envelope(), {"reltol": 1e-05})

    path, item = _graded(tmp_path, envelope, {"ngspice_options": {"reltol": 0.001}})

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["requires_unmet"] == [
        {
            "member": path,
            "ngspice_options": {
                "required": {"reltol": 0.001},
                "observed": {"reltol": 1e-05},
            },
        }
    ]


@pytest.mark.parametrize("echo", [None, {}])
def test_an_absent_options_echo_never_credits(tmp_path, echo):
    envelope = _with_options(_grid_envelope(), echo)

    _, item = _graded(tmp_path, envelope, {"ngspice_options": {"reltol": 0.001}})

    assert item["status"] == "unmet"
    entry = item["detail"]["requires_unmet"][0]["ngspice_options"]
    assert entry == {"required": {"reltol": 0.001}, "observed": None}


def test_a_required_option_the_run_did_not_declare_is_unmet(tmp_path):
    envelope = _with_options(_grid_envelope(), {"abstol": 1e-12})

    _, item = _graded(tmp_path, envelope, {"ngspice_options": {"reltol": 0.001}})

    assert item["detail"]["requires_unmet"][0]["ngspice_options"]["observed"] == {
        "abstol": 1e-12
    }


def test_a_bool_option_never_equals_a_number(tmp_path):
    # True == 1 in Python, not in JSON: a bool echo must not satisfy a number.
    envelope = _with_options(_grid_envelope(), {"gminsteps": 1})

    _, item = _graded(tmp_path, envelope, {"ngspice_options": {"gminsteps": True}})

    assert item["status"] == "unmet"


def test_grid_and_options_shortfalls_are_reported_together(tmp_path):
    envelope = _with_options(_grid_envelope(drop=("ff", 1.98, -40)), {"reltol": 1e-05})

    _, item = _graded(
        tmp_path,
        envelope,
        {"grid": _GRID, "ngspice_options": {"reltol": 0.001}},
    )

    (entry,) = item["detail"]["requires_unmet"]
    assert entry["missing_points"] == ["ff/1.980V/-40C"]
    assert entry["ngspice_options"]["observed"] == {"reltol": 1e-05}


# --------------------------------------------------------------------------- #
# Non-regression: no `requires` is byte-identical; the verdict is additive.
# --------------------------------------------------------------------------- #


def test_no_requires_key_leaves_the_item_untouched(tmp_path):
    path = _write(tmp_path, "probe.json", _reduced_envelope())

    item = _item5({"5": [{"file": path}]})

    assert item["status"] == "met"
    assert "requires_unmet" not in item.get("detail", {})
    assert all("requires" not in g for g in item["citations_graded"])


def test_a_members_own_failure_keeps_its_reason(tmp_path):
    failing = {**_grid_envelope(), "status": "fail", "failed": 1, "passed": 26}

    _, item = _graded(tmp_path, failing, {"grid": _GRID})

    assert item["status"] == "unmet"
    assert item["reason"] != "partial_coverage"
    assert "requires_unmet" not in item["detail"]


def test_requires_composes_with_a_second_unbound_member(tmp_path):
    probe = _write(tmp_path, "probe.json", _reduced_envelope())
    full = _write(tmp_path, "full.json", _grid_envelope())

    item = _item5({"5": [{"file": full, "requires": {"grid": _GRID}}, {"file": probe}]})

    assert item["status"] == "met"  # only the bound member is held to the grid


# --------------------------------------------------------------------------- #
# invalid_evidence: malformed requires, unsupported items, Monte Carlo.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "requires",
    [
        "full",
        ["grid"],
        {},
        {"bogus": 1},
        {"grid": _GRID, "bogus": 1},
        {"grid": "all"},
        {"grid": {}},
        {"grid": {"process": None}},
        {"grid": {"bogus": [1]}},
        {"grid": {"process": []}},
        {"grid": {"process": ["tt", ""]}},
        {"grid": {"temperature_c": []}},
        {"grid": {"temperature_c": ["hot"]}},
        {"grid": {"temperature_c": [True]}},
        {"grid": {"supply_v": {}}},
        {"grid": {"supply_v": {"vdd": []}}},
        {"grid": {"supply_v": {"vdd": ["1.8"]}}},
        {"grid": {"supply_v": {"vdd": [1.8], "vio": [1.8, 3.3]}}},
        {"ngspice_options": {}},
        {"ngspice_options": ["reltol"]},
        {"ngspice_options": {"reltol": [0.001]}},
        {"ngspice_options": {"reltol": None}},
        {"ngspice_options": {"reltol": ""}},
    ],
)
def test_a_malformed_requires_is_invalid_evidence_never_a_raise(tmp_path, requires):
    path = _write(tmp_path, "a.json", _grid_envelope())

    item = _item5({"5": [{"file": path}, {"file": path, "requires": requires}]})

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"].startswith("member 2 'requires")


def test_a_lone_citation_names_the_entry_in_the_problem(tmp_path):
    path = _write(tmp_path, "a.json", _grid_envelope())

    item = _item5({"5": {"file": path, "requires": 3}})

    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"] == "entry 'requires' must be an object"


def test_a_malformed_requires_refuses_before_any_citation_is_read(tmp_path):
    # An unreadable file would otherwise grade `unreadable_evidence`.
    item = _item5({"5": [{"file": "nowhere.json", "requires": {"bogus": 1}}]})

    assert item["reason"] == "invalid_evidence"


def test_a_monte_carlo_member_under_requires_grid_is_unsupported(tmp_path):
    path = _write(tmp_path, "mc.json", _grid_envelope(monte_carlo=True))

    item = _item5({"5": [{"file": path, "requires": {"grid": _GRID}}]})

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert path in item["detail"]["problem"]
    assert "Monte Carlo" in item["detail"]["problem"]


def test_a_monte_carlo_member_may_still_bind_its_solver_profile(tmp_path):
    envelope = _with_options(_grid_envelope(monte_carlo=True), OPTIONS)

    _, item = _graded(tmp_path, envelope, {"ngspice_options": {"reltol": 0.001}})

    assert item["status"] == "met"


@pytest.mark.parametrize("item_id", [3, 6, 7])
def test_requires_on_another_item_is_invalid_evidence(tmp_path, item_id):
    path = _write(tmp_path, "a.json", _grid_envelope())
    manifest = {
        "block": "demo-block",
        "kind": "analog",
        "evidence": {str(item_id): [{"file": path, "requires": {"grid": _GRID}}]},
    }

    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == item_id)

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"] == "'requires' is supported on item 5 only"


# --------------------------------------------------------------------------- #
# The shared helpers.
# --------------------------------------------------------------------------- #


def test_read_requires_keeps_the_declared_values_verbatim():
    declared = {"grid": _GRID, "ngspice_options": {"reltol": 0.001}}

    requires, problem = read_requires(declared)

    assert problem is None
    assert requires == declared
    assert requires is not declared  # a copy, not the manifest's own object


def test_missing_grid_points_is_the_grid_half_of_member_shortfall():
    envelope = _grid_envelope(drop=("ss", 1.8, 125))

    assert missing_grid_points(envelope, _GRID) == ["ss/1.800V/125C"]
    assert member_shortfall(envelope, _GRID, "pvt_grid")[0] == ["ss/1.800V/125C"]


def test_an_unavailable_envelope_misses_every_grid_point():
    assert len(missing_grid_points(None, _GRID)) == 27
