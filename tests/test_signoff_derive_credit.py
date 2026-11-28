"""A cross-corner ``measurements[].derive`` entry credits a `klt signoff`
item-5 row like a per-corner measurement does (issue #7184).

Split out of ``test_signoff_derived_rows.py`` to stay under the per-file size
ratchet; the shared fixtures and helpers still live there. The envelopes are
built with `klt sim`'s own rollup, coverage and grading (no ngspice), so the
work ids the credit reads are the producer's, not a hand-written copy.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_derive
from klayout_tools.coverage import work_id
from klayout_tools.signoff_derived_bounds import limit_work_names
from test_signoff_derived_rows import SIM_PASS_ENVELOPE, _item5, _write


def _derive_envelope(min_limit: float = 0.20, **flags: bool) -> dict:
    """Two supply groups over a 3-point code axis; the unbounded source ``v``
    is 0.80/1.00/1.15 V at 1.0 V and 1.80/2.00/2.30 V at 2.0 V, so the row is
    0.35 and 0.25. ``drop_point`` excludes one (group, code) corner;
    ``sibling_fails`` adds a limited measurement that misses its bound;
    ``unrecognised_bound`` declares a second, unrecognised limit key, which
    `klt sim` skips (``unrecognized_limit_key``) beside the checked ``min``."""
    spec = {
        "name": "code_range_rel",
        "derive": {
            "from": "v",
            "axis": "params.code",
            "reduce": "range_rel",
            "reference": {"params.code": 128},
            "group_by": ["supply_v"],
        },
        "limits": {
            "min": min_limit,
            **({"bogus": 1.0} if flags.get("unrecognised_bound") else {}),
        },
        "spec_row": "trim-range",
    }
    sibling = {"name": "vdd_ok", "limits": {"max": 1.0}, "spec_row": "vdd-row"}
    specs = [{"name": "v", "expr": "v(out)"}, spec]
    if flags.get("sibling_fails"):
        specs.append(sibling)
    corners_spec = {"supply_v": {"vdd": [1.0, 2.0]}, "params": {"code": [0, 128, 255]}}
    exclude = (
        [{"supply_v": {"vdd": 1.0}, "params": {"code": 128}}]
        if flags.get("drop_point")
        else []
    )
    sim_derive.validate_grid(
        specs, sim.expand_corners(corners_spec, exclude), {"corners": corners_spec}
    )
    values = {1.0: (0.80, 1.00, 1.15), 2.0: (1.80, 2.00, 2.30)}
    corners = [
        {
            "corner_id": f"default/{supply}V/27C/params:code={code}",
            "supply_v": {"vdd": supply},
            "temperature_c": 27,
            "params": {"code": code},
            "status": "fail" if sibling in specs else "pass",
            "measurements": [
                {"name": "v", "value": value, "status": "pass", "margin": None}
            ]
            + (
                [{"name": "vdd_ok", "value": 1.2, "status": "fail", "margin": -0.2}]
                if sibling in specs
                else []
            ),
            "diagnostics": [],
        }
        for supply, row in values.items()
        for code, value in zip((0, 128, 255), row, strict=True)
        if not (exclude and supply == 1.0 and code == 128)
    ]
    measurements = sim._rollup_measurements(specs, corners)
    coverage = sim._build_coverage(measurements, corners)
    failed = sum(c["status"] == "fail" for c in corners)
    return {
        **SIM_PASS_ENVELOPE,
        "status": sim._grade_corners(corners, measurements, coverage)["status"],
        "corner_count": len(corners),
        "passed": len(corners) - failed,
        "failed": failed,
        "corners": corners,
        "measurements": measurements,
        "coverage": coverage,
    }


def _item5_for(tmp_path, envelope: dict, rows: list[str], **flags: bool) -> dict:
    path = _write(tmp_path, "a.json", envelope)
    return _item5(
        {"5": [{"file": path, "rows": rows}]},
        [{"id": row} for row in rows],
        **flags,
    )


def test_item5_require_derived_rows_credits_a_passing_derive_measurement(tmp_path):
    envelope = _derive_envelope()
    assert envelope["status"] == "pass"
    assert envelope["coverage"]["measurements_with_limits"] == 1

    item = _item5_for(tmp_path, envelope, ["trim-range"], require_derived_rows=True)

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"trim-range": "derived"}


def test_item5_failing_envelope_credits_the_derive_row_that_passed(tmp_path):
    # Issue #6027: the sibling failed while the derive row passed.
    envelope = _derive_envelope(sibling_fails=True)
    assert envelope["status"] == "fail"
    assert [m["status"] for m in envelope["measurements"]] == ["pass", "pass", "fail"]

    item = _item5_for(tmp_path, envelope, ["trim-range", "vdd-row"])

    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["vdd-row"]
    assert item["detail"]["row_sources"] == {"trim-range": "derived"}


@pytest.mark.parametrize("flags", [{"min_limit": 0.30}, {"drop_point": True}])
def test_item5_never_credits_a_failing_or_errored_derive_measurement(tmp_path, flags):
    envelope = _derive_envelope(**flags)
    assert envelope["measurements"][1]["status"] in {"fail", "error"}

    item = _item5_for(tmp_path, envelope, ["trim-range"])

    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_rows"] == ["trim-range"]
    assert item["detail"]["row_sources"] == {}


def test_item5_failing_derive_with_an_unchecked_group_bound_is_not_credited(tmp_path):
    envelope = _derive_envelope(sibling_fails=True)
    bound = work_id("derived_limit", "code_range_rel", "supply_v=2.000V", "min")
    assert bound in envelope["coverage"]["checked"]
    envelope["coverage"]["checked"].remove(bound)

    item = _item5_for(tmp_path, envelope, ["trim-range", "vdd-row"])

    assert item["detail"]["failed_rows"] == ["trim-range", "vdd-row"]
    assert item["detail"]["row_sources"] == {}


def test_limit_work_names_reads_the_name_out_of_both_bound_id_shapes():
    # `limit:[corner_index, corner_id, name, key]` and
    # `derived_limit:[name, group_id, key]`: the ids are the producer's own.
    per_corner = work_id("limit", 0, "tt/1.800V/27C", "vout", "min")
    derived = work_id("derived_limit", "code_range_rel", "supply_v=1.000V", "min")

    assert limit_work_names([per_corner]) == {"vout"}
    assert limit_work_names([derived]) == {"code_range_rel"}
    assert limit_work_names([per_corner, derived]) == {"vout", "code_range_rel"}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        7,
        "measurement:[1]",
        "derived_limit:not-json",
        # The per-corner width on a derived id, and the reverse.
        work_id("derived_limit", "n", "g", "min", "extra"),
        work_id("limit", "vout", "min"),
        # A non-string name.
        work_id("derived_limit", 3, "g", "min"),
        'derived_limit:{"name":"n"}',
    ],
)
def test_limit_work_names_ignores_ids_it_cannot_read(raw):
    assert limit_work_names([raw]) == set()


def test_item5_failing_derive_with_a_skipped_group_bound_is_not_credited(tmp_path):
    # The unrecognised `bogus` bound is skipped while `min` is checked at every
    # group: the entry's name must still be read back out of the skipped
    # `derived_limit` id, exactly as a skipped per-corner `limit` withholds the
    # row, or the checked `min` alone would credit it.
    envelope = _derive_envelope(sibling_fails=True, unrecognised_bound=True)
    skipped = {r["id"]: r["reason"] for r in envelope["coverage"]["skipped"]}
    bogus = work_id("derived_limit", "code_range_rel", "supply_v=1.000V", "bogus")
    assert skipped[bogus] == "unrecognized_limit_key"
    for supply in ("supply_v=1.000V", "supply_v=2.000V"):
        checked = work_id("derived_limit", "code_range_rel", supply, "min")
        assert checked in envelope["coverage"]["checked"]
    assert [m["status"] for m in envelope["measurements"]][1] == "pass"

    item = _item5_for(tmp_path, envelope, ["trim-range", "vdd-row"])

    assert item["detail"]["failed_rows"] == ["trim-range", "vdd-row"]
    assert item["detail"]["row_sources"] == {}
