"""``measurements[].derive`` for ``klt sim`` (issue #7184): a deterministic
figure reduced across the corners of one request along one swept axis.

Request validation (the form, its body, the corner grid) and the four
reductions' known answers. The reduction is pure Python over the reported
corners, so the known-answer tests feed hand-built corner reports straight to
``sim._rollup_measurements`` with exact IEEE-754 expectations; the pipeline
itself (decks, harvest, coverage, grading, exit codes) is covered by
``tests/test_sim_derive_run.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from klayout_tools import sim, sim_derive, sim_validate
from klayout_tools.sim_errors import SimError

_CODE = {"from": "vout", "axis": "params.code", "reduce": "range", "group_by": []}
#: The issue's repro sketch, per (process, supply) group: ``v`` at code 0/128/255.
GROUPS = {("a", 1.0): [0.80, 1.00, 1.15], ("b", 2.0): [1.80, 2.00, 2.30]}
CORNERS = {
    "process": ["a", "b"],
    "supply_v": {"vdd": [1.0, 2.0]},
    "params": {"code": [0, 128, 255]},
}


def test_standalone_range_rollup():
    probe = """
import sys
from klayout_tools.sim_derive_rollup import rollup_fields

spec = {"name": "span", "derive": {
    "from": "vout", "axis": "params.code", "group_by": [], "reduce": "range",
}}
corners = [
    {"params": {"code": code}, "status": "pass",
     "measurements": [{"name": "vout", "status": "pass", "value": value}]}
    for code, value in enumerate((1.0, 3.0))
]
group, = rollup_fields(spec, corners)["derive"]["groups"]
assert (group["value"], group["status"]) == (2.0, "pass")
assert "klayout_tools.sim" not in sys.modules
"""
    subprocess.run([sys.executable, "-c", probe], check=True)


def derive(**body) -> dict:
    return {"name": "d", "derive": {**_CODE, **body}}


def validate(spec: dict) -> None:
    sim_validate._validate_measurement_forms([{"name": "vout", "expr": "v(out)"}, spec])


def grid(values: dict, *, name: str = "vout") -> list[dict]:
    """Corner reports for ``{(process, supply): [v(code=0), v(128), v(255)]}``."""
    return [
        {
            "corner_id": f"{process}/{supply}V/27C/params:code={code}",
            "process": process,
            "supply_v": {"vdd": supply},
            "temperature_c": 27,
            "params": {"code": code},
            "status": "pass",
            "measurements": [
                {"name": name, "value": value, "status": "pass", "margin": None}
            ],
            "diagnostics": [],
        }
        for (process, supply), row in values.items()
        for code, value in zip((0, 128, 255), row, strict=True)
    ]


def entry(spec: dict, corners: list[dict]) -> dict:
    """``spec``'s rollup entry, beside an unbounded source measurement."""
    rollup = sim._rollup_measurements(
        [{"name": "vout", "expr": "v(out)"}, spec], corners
    )
    return next(e for e in rollup if e["name"] == spec["name"])


def with_source(spec: dict) -> list[dict]:
    """``spec`` beside an unbounded measurement named by its ``derive.from``."""
    return [{"name": spec["derive"]["from"], "expr": "v(out)"}, spec]


def on_grid(spec: dict, corners: dict | None = None, **request) -> dict:
    """``spec`` after :func:`sim_derive.validate_grid` on the declared grid."""
    corners = CORNERS if corners is None else corners
    sim_derive.validate_grid(
        with_source(spec),
        sim.expand_corners(corners, []),
        {"corners": corners, **request},
    )
    return spec


CODE_ONLY = {"params": {"code": [0, 128, 255]}}


def one_group(corners: list[dict], spec: dict, declared: dict = CODE_ONLY) -> dict:
    """The single group of a ``spec`` reduced over one ``(process, supply)``
    group's corners, validated against the code-only grid by default."""
    (group,) = entry(on_grid(spec, declared), corners)["derive"]["groups"]
    return group


# --------------------------------------------------------------------------- #
# Request validation: the form and its body
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not-an-object", "must be an object"),
        ({"from": "vout", "extra": 1}, "unknown key"),
        ({"from": "vout"}, r"requires \['axis', 'reduce', 'group_by'\]"),
        ({**_CODE, "from": " "}, "derive.from must be the name"),
        ({**_CODE, "axis": "bogus"}, "unknown axis"),
        ({**_CODE, "axis": "params"}, "must name a key"),
        ({**_CODE, "axis": "temperature_c.x"}, "malformed"),
        ({**_CODE, "reduce": "mean"}, "derive.reduce must be one of"),
        ({**_CODE, "reduce": "max_abs_step", "axis": "process"}, "has no order"),
        ({**_CODE, "reduce": "box_spread", "axis": "variant"}, "has no order"),
        ({**_CODE, "group_by": "process"}, "must be an array"),
        ({**_CODE, "group_by": ["params.code"]}, "names the reduced axis"),
        ({**_CODE, "group_by": ["process", "Process"]}, "repeats an axis"),
        ({**_CODE, "group_by": ["nope"]}, "unknown axis"),
        ({**_CODE, "reference": {"params.code": 128}}, "only apply to"),
        ({**_CODE, "divisor": 2}, "only apply to"),
        ({**_CODE, "reduce": "range_rel"}, "exactly one of"),
        (
            {
                **_CODE,
                "reduce": "range_rel",
                "reference": {"params.code": 128},
                "divisor": 2,
            },
            "exactly one of",
        ),
        ({**_CODE, "reduce": "range_rel", "divisor": 0}, "positive finite"),
        ({**_CODE, "reduce": "range_rel", "divisor": -1}, "positive finite"),
        ({**_CODE, "reduce": "range_rel", "divisor": True}, "positive finite"),
        ({**_CODE, "reduce": "range_rel", "divisor": "2"}, "positive finite"),
        ({**_CODE, "reduce": "range_rel", "reference": {}}, "exactly one"),
        ({**_CODE, "reduce": "range_rel", "reference": {"process": "a"}}, "names axis"),
        (
            {**_CODE, "reduce": "range_rel", "reference": {"params.code": "128"}},
            "finite",
        ),
        (
            {**_CODE, "reduce": "range_rel", "reference": {"params.code": None}},
            "finite",
        ),
        (
            {
                **_CODE,
                "reduce": "range_rel",
                "axis": "process",
                "reference": {"process": 3},
            },
            "name",
        ),
    ],
)
def test_malformed_derive_is_rejected(body, message):
    with pytest.raises(SimError, match=message):
        validate({"name": "d", "derive": body})


@pytest.mark.parametrize("key", ["optional", "on_unreached", "k_sigma", "intermediate"])
def test_per_corner_only_keys_are_refused(key):
    with pytest.raises(SimError, match=f"'{key}' is not supported on a 'derive'"):
        validate({**derive(), key: True})
    if key in ("optional", "intermediate"):  # a false flag is the default
        validate({**derive(), key: False})


def test_derive_is_an_exclusive_form():
    assert "derive" in sim_validate.sim_forms.EXCLUSIVE_FORMS
    with pytest.raises(SimError, match="alternative forms"):
        validate({"name": "d", "derive": dict(_CODE), "expr": "v(out)"})
    with pytest.raises(SimError, match="alternative forms"):
        validate({"name": "d", "derive": dict(_CODE), "spice": ".meas op x v(out)"})


def test_valid_derive_normalizes_axis_names_in_place():
    spec = derive(
        axis="Params.Code",
        group_by=["Process", "SUPPLY_V.vdd"],
        reduce="range_rel",
        reference={"params.CODE": 128},
    )
    validate(spec)
    assert spec["derive"]["axis"] == "params.code"
    assert spec["derive"]["group_by"] == ["process", "supply_v.vdd"]
    assert spec["derive"]["reference"] == {"params.code": 128}


def test_per_corner_view_of_the_spec_list():
    specs = [{"name": "vout", "expr": "v(out)"}, derive()]
    assert sim_derive.per_corner_measurements(specs) == specs[:1]
    assert sim_derive.derive_measurements(specs) == specs[1:]


# --------------------------------------------------------------------------- #
# The four reductions, per group
# --------------------------------------------------------------------------- #


def test_issue_repro_sketch_range_rel_grades_each_group_and_fails_the_envelope():
    spec = on_grid(
        {
            "name": "code_range_rel",
            "derive": {
                "from": "vout",
                "axis": "params.code",
                "reduce": "range_rel",
                "reference": {"params.code": 128},
                "group_by": ["process", "supply_v"],
            },
            "limits": {"min": 0.30},
            "spec_row": "trim-range",
        }
    )
    rollup = entry(spec, grid(GROUPS))
    a, b = rollup["derive"]["groups"]
    assert a["group_id"] == "process=a/supply_v=1.000V"
    assert a["value"] == (1.15 - 0.80) / 1.00
    assert (a["status"], a["margin"], a["reason"]) == (
        "pass",
        pytest.approx(0.05),
        None,
    )
    assert b["value"] == pytest.approx(0.25) and b["status"] == "fail"
    assert rollup["status"] == "fail"
    assert rollup["spec_row"] == "trim-range"
    assert rollup["worst_case"] == {
        "corner_id": b["group_id"],
        "group_id": b["group_id"],
        "group": {"process": "b", "supply_v": 2.0},
        "value": b["value"],
        "margin": b["margin"],
    }
    assert rollup["min"]["group_id"] == b["group_id"]
    assert rollup["max"]["group_id"] == a["group_id"]
    block = rollup["derive"]
    assert block["expected_axis_values"] == [0, 128, 255]
    assert {
        k: block[k] for k in block if k not in ("groups", "expected_axis_values")
    } == {
        "from": "vout",
        "axis": "params.code",
        "reduce": "range_rel",
        "group_by": ["process", "supply_v"],
        "reference": {"params.code": 128},
    }


def test_range_and_range_rel_with_a_divisor_over_an_unordered_axis():
    values = {("a", 1.0): [0.5, 0.5, 2.5], ("b", 1.0): [4.0, 4.0, 4.0]}
    declared = {**CODE_ONLY, "process": ["a", "b"], "supply_v": {"vdd": [1.0]}}
    rng = entry(
        on_grid(derive(axis="process", group_by=["params.code"]), declared),
        grid(values),
    )
    # Per code setting, across the (unordered) process axis: 4.0 - 0.5, 4.0 - 0.5,
    # then 4.0 - 2.5.
    assert [g["value"] for g in rng["derive"]["groups"]] == [3.5, 3.5, 1.5]
    rel = entry(
        on_grid(
            derive(
                axis="process",
                reduce="range_rel",
                divisor=2.0,
                group_by=["params.code"],
            ),
            declared,
        ),
        grid(values),
    )
    assert rel["derive"]["groups"][0]["value"] == 1.75
    assert rel["derive"]["divisor"] == 2.0


def test_range_rel_uses_the_absolute_reference_value():
    corners = grid({("a", 1.0): [-2.0, 0.5, -1.0]})
    spec = derive(reduce="range_rel", reference={"params.code": 0})
    assert one_group(corners, spec)["value"] == 2.5 / 2.0


def test_box_spread_over_temperature():
    corners = [
        {
            "corner_id": f"tt/1.800V/{t}C",
            "process": "tt",
            "supply_v": {"vdd": 1.8},
            "temperature_c": t,
            "status": "pass",
            "measurements": [
                {"name": "vbg", "value": v, "status": "pass", "margin": None}
            ],
            "diagnostics": [],
        }
        for t, v in zip((-40, 27, 125), (1.198, 1.200, 1.204), strict=True)
    ]
    declared = {
        "process": ["tt"],
        "supply_v": {"vdd": [1.8]},
        "temperature_c": [-40, 27, 125],
    }

    def spread(limit: float) -> dict:
        spec = {
            "name": "vbg_tc",
            "derive": {
                "from": "vbg",
                "axis": "temperature_c",
                "reduce": "box_spread",
                "reference": {"temperature_c": 27},
                "group_by": ["process", "supply_v"],
            },
            "unit": "1/C",
            "limits": {"max": limit},
        }
        return entry(on_grid(spec, declared), corners)

    passing = spread(50e-6)
    (group,) = passing["derive"]["groups"]
    assert group["group_id"] == "process=tt/supply_v=1.800V"
    assert group["value"] == (1.204 - 1.198) / ((125 - -40) * 1.200)
    assert group["value"] == pytest.approx(3.0303e-5, rel=1e-4)
    assert passing["status"] == "pass"
    failing = spread(20e-6)
    assert failing["status"] == "fail"
    assert failing["worst_case"]["margin"] == pytest.approx(20e-6 - group["value"])


def test_max_abs_step_sorts_the_axis_regardless_of_declaration_order():
    spec = on_grid(derive(reduce="max_abs_step"), {"params": {"code": [255, 0, 128]}})
    corners = [
        {
            "corner_id": f"default/novdd/27C/params:code={code}",
            "process": None,
            "supply_v": {},
            "temperature_c": 27,
            "params": {"code": code},
            "status": "pass",
            "measurements": [
                {"name": "vout", "value": v, "status": "pass", "margin": None}
            ],
            "diagnostics": [],
        }
        for code, v in zip((255, 0, 128), (1.30, 1.00, 1.25), strict=True)
    ]
    group = one_group(corners, spec)
    assert (group["group_id"], group["value"]) == ("all", 0.25)


def test_limits_are_inclusive_at_equality():
    spec = {"name": "d", "derive": dict(_CODE), "limits": {"min": 1.0, "max": 1.0}}
    corners = grid({("a", 1.0): [0.0, 1.0, 1.0]})[:2]
    group = one_group(corners, spec, {"params": {"code": [0, 128]}})
    assert (group["status"], group["margin"]) == ("pass", 0.0)


# --------------------------------------------------------------------------- #
# An untrusted group never passes
# --------------------------------------------------------------------------- #


def test_a_missing_axis_point_is_an_error_naming_the_values():
    corners = [c for c in grid(GROUPS) if c["params"]["code"] != 128]
    for group in entry(on_grid(derive(group_by=["process", "supply_v"])), corners)[
        "derive"
    ]["groups"]:
        assert (group["status"], group["value"], group["margin"]) == (
            "error",
            None,
            None,
        )
        assert (group["reason"], group["missing_axis_values"]) == (
            "missing_axis_points",
            [128],
        )


@pytest.mark.parametrize(
    ("status", "value"),
    [
        ("error", None),
        ("not_reached", None),
        ("absent", None),
        ("pass", float("nan")),
        ("pass", None),
    ],
)
def test_an_unavailable_source_is_an_error(status, value):
    corners = grid({("a", 1.0): [0.8, 1.0, 1.15]})
    corners[1]["measurements"][0].update(status=status, value=value)
    group = one_group(corners, derive())
    assert (group["status"], group["reason"]) == ("error", "source_unavailable")


def test_an_errored_corner_is_unavailable_whatever_its_measurement_reads():
    corners = grid({("a", 1.0): [0.8, 1.0, 1.15]})
    corners[0]["status"] = "error"
    assert one_group(corners, derive())["reason"] == "source_unavailable"


@pytest.mark.parametrize(
    ("reduction", "divisor", "positions", "values", "limits"),
    [
        ("range_rel", 1e-310, [0, 1, 2], [0.5, 1.0, 0.75], {"min": 0}),
        ("range", None, [0, 1, 2], [-1e308, 0.0, 1e308], {}),
        ("max_abs_step", None, [0, 1, 2], [-1e308, 1e308, 0.0], {}),
        ("box_spread", 5e-324, [1.0, 1.05, 1.1], [0.5, 1.0, 0.75], {}),
        ("box_spread", 1e308, [0, 1, 2], [0.5, 1.0, 0.75], {}),
        ("range", None, [0, 1, 2], [0.0, 5e307, 1e308], {"min": -1e308}),
    ],
)
def test_unrepresentable_derived_arithmetic_is_a_serializable_group_error(
    reduction, divisor, positions, values, limits
):
    corners = grid({("a", 1.0): values})
    for corner, coordinate in zip(corners, positions, strict=True):
        corner["params"]["code"] = coordinate
    body = {"reduce": reduction}
    if divisor is not None:
        body["divisor"] = divisor
    spec = {**derive(**body), "limits": limits}
    declared = {"params": {"code": positions}}
    report = entry(on_grid(spec, declared), corners)
    json.dumps(report, allow_nan=False)
    group = report["derive"]["groups"][0]
    assert group["status"] == "error" and group["reason"] == "numeric_range"
    assert group["value"] is None and group["margin"] is None


def test_tiny_divisor_with_representable_result_remains_gradeable():
    corners = grid({("a", 1.0): [0.5, 1.0, 0.75]})
    group = one_group(corners, derive(reduce="range_rel", divisor=1e-308))
    assert group["status"] == "pass" and group["value"] == 0.5 / 1e-308


def test_a_zero_reference_is_an_error_not_a_pass():
    spec = derive(reduce="range_rel", reference={"params.code": 128})
    group = one_group(grid({("a", 1.0): [0.8, 0.0, 1.15]}), spec)
    assert (group["status"], group["reason"]) == ("error", "zero_reference")


def test_an_inconclusive_input_grades_inconclusive_and_outranks_a_clean_fail():
    spec = on_grid(
        {
            "name": "d",
            "derive": {
                "from": "vout",
                "axis": "params.code",
                "reduce": "range_rel",
                "reference": {"params.code": 128},
                "group_by": ["process", "supply_v"],
            },
            "limits": {"min": 0.30},
        }
    )
    corners = grid(GROUPS)
    corners[1]["status"] = "inconclusive"  # (a, 1.0 V) at code 128
    rollup = entry(spec, corners)
    assert rollup["status"] == "inconclusive"
    assert rollup["worst_case"]["group_id"] == "process=a/supply_v=1.000V"
    by_id = {g["group_id"]: g for g in rollup["derive"]["groups"]}
    assert by_id["process=a/supply_v=1.000V"]["reason"] == "source_inconclusive"
    assert by_id["process=b/supply_v=2.000V"]["status"] == "fail"
    # ... and an error outranks both: one group missing a point, one inconclusive.
    corners[4]["status"] = "inconclusive"  # (b, 2.0 V) at code 128
    rollup = entry(spec, corners[:2] + corners[3:5] + corners[5:])
    assert rollup["status"] == "error"


def test_the_plausible_range_applies_to_the_derived_value():
    spec = {**derive(), "plausible_range": {"min": 0.0, "max": 0.2}}
    group = one_group(
        grid({("a", 1.0): [0.0, 0.5, 0.5]})[:2], spec, {"params": {"code": [0, 128]}}
    )
    assert (group["status"], group["margin"], group["reason"]) == (
        "inconclusive",
        None,
        "implausible_value",
    )
