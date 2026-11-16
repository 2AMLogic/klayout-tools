"""Identical-process-leg diagnostic and value table (#5385)."""

from klayout_tools import sim, sim_process_legs


def _corner(leg, value, supply=1.8, temp=27, suffix="", name="gain", status="pass"):
    return {
        "corner_id": f"{leg}/{supply:.3f}V/{temp}C{suffix}",
        "process": leg,
        "supply_v": {"vdd": supply},
        "temperature_c": temp,
        "monte_carlo": None,
        "measurements": [
            {"name": name, "value": value, "margin": None, "status": status}
        ],
    }


def test_identical_legs_flagged():
    corners = [_corner("tt", 1.0), _corner("ss", 1.0), _corner("ff", 2.0)]
    diag = sim_process_legs.process_leg_check(corners)
    assert diag["code"] == "process_leg_check"
    assert diag["severity"] == "warning"
    assert diag["legs"] == [["ss", "tt"]]


def test_differing_legs_and_single_leg_emit_nothing():
    assert (
        sim_process_legs.process_leg_check([_corner("tt", 1), _corner("ss", 2)]) is None
    )
    assert sim_process_legs.process_leg_check([_corner("tt", 1)]) is None


def test_identical_only_if_every_point_matches():
    corners = [
        _corner("tt", 1.0),
        _corner("ss", 1.0),
        _corner("tt", 1.0, temp=85),
        _corner("ss", 1.5, temp=85),
    ]
    assert sim_process_legs.process_leg_check(corners) is None


def test_non_finite_and_absent_ignored():
    corners = [
        _corner("tt", float("nan")),
        _corner("ss", None, status="absent"),
    ]
    assert sim_process_legs.process_leg_check(corners) is None


def test_mc_samples_group_by_index():
    corners = [
        _corner("tt", 1.0, suffix="/mc0"),
        _corner("tt", 2.0, suffix="/mc1"),
        _corner("ss", 1.0, suffix="/mc0"),
        _corner("ss", 2.0, suffix="/mc1"),
    ]
    assert sim_process_legs.process_leg_check(corners)["legs"] == [["ss", "tt"]]


def test_min_max_name_later_leg_while_worst_case_is_first():
    corners = [_corner("tt", 1.0), _corner("ss", 0.5), _corner("ff", 2.0)]
    (entry,) = sim._rollup_measurements([{"name": "gain"}], corners)
    assert entry["worst_case"]["corner_id"] == corners[0]["corner_id"]
    assert entry["min"]["corner_id"].startswith("ss/")
    assert entry["max"]["corner_id"].startswith("ff/")


def test_value_table_rows():
    corners = [_corner("tt", 1.0), _corner("ss", 0.5)]
    assert sim_process_legs.value_table("gain", corners) == [
        ("tt/1.800V/27C", 1.0),
        ("ss/1.800V/27C", 0.5),
    ]
