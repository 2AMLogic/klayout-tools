"""Issue #5490: a `klt yield` measurement that declares no `target_yield` has
`status: "reported"` -- it made no claim, so nothing could fail. Item 6 used
to credit a spec row only from a `status: "pass"` tagged measurement, so such
a citation left its row in `detail.uncovered_rows`, reading as "nobody
measured this".

That is exactly backwards for the row the flag `ratified: false` (issue
#2908) exists for: a row whose spec states a method rather than a number has
no yield claim to make, so declaring a `target_yield` to satisfy the grader
would invent one. These tests pin the scoped rule -- a tagged,
`limits`-bearing `reported` measurement covers its row **when that row is
unratified**, and nothing else moves.

The item-6 fixtures and the full-T1 manifest helper live in `test_signoff`;
they are imported here rather than duplicated (the same convention
`test_extract_split_labels` and friends use).
"""

from __future__ import annotations

import pytest

from test_signoff import _item6_rows, _write, _yield_envelope, _yield_measurement


def _yield_meas_tagged(
    name: str,
    spec_row: str,
    *,
    status: str = "reported",
    limits: bool = True,
) -> dict:
    """One `klt yield` measurement tagged with the spec row it grades.

    `status="reported"` is the shape a measurement declaring no
    `target_yield` has (docs/cli/yield.md's `status` field): it still
    carries the window it was measured against, but no yield claim that
    could fail. `limits=False` drops the window entirely, the one case
    that grades nothing whatever the status.
    """
    measurement = {**_yield_measurement(name=name), "spec_row": spec_row}
    measurement["status"] = status
    if not limits:
        measurement.pop("limits", None)
    elif status == "reported":
        # No `target_yield` is exactly why the status is `reported`.
        measurement["limits"] = {"min": 1.15, "max": 1.25}
    return measurement


def test_item6_reported_measurement_covers_an_unratified_row(tmp_path):
    """The issue-#5490 case: a real campaign cited for a row whose limit
    nobody has ratified declares no `target_yield`, so its measurement is
    `reported`. That is "measured, against an unratified limit", not
    "unmeasured" -- the row is covered, and still provisional."""
    envelope = _yield_envelope(_yield_meas_tagged("vref", "row_a"), status="reported")
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", "ratified": False}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["uncovered_rows"] == []
    assert item["detail"]["unratified_rows"] == ["row_a"]
    assert item["detail"]["row_sources"] == {"row_a": "derived"}


def test_item6_reported_measurement_does_not_cover_a_ratified_row(tmp_path):
    """A ratified row still needs a measurement that graded against its
    target: crediting a no-claim campaign there would loosen the #3778
    gate rather than fix a misleading list."""
    envelope = _yield_envelope(_yield_meas_tagged("vref", "row_a"), status="reported")
    a = _write(tmp_path, "mc_a.json", envelope)

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], [{"id": "row_a"}])

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["uncovered_rows"] == ["row_a"]
    assert item["detail"]["row_sources"] == {}


@pytest.mark.parametrize("status", ["fail", "insufficient_evidence"])
@pytest.mark.parametrize("ratified", [True, False])
def test_item6_non_reported_failing_measurement_never_covers(
    tmp_path, status, ratified
):
    """Only `pass` and `reported` are no-failure tokens; `fail` and
    `insufficient_evidence` credit no row, ratified or not."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a", status=status), status="reported"
    )
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", **({} if ratified else {"ratified": False})}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], rows)

    assert item["status"] == "unmet"
    assert item["detail"]["uncovered_rows"] == ["row_a"]


@pytest.mark.parametrize("ratified", [True, False])
def test_item6_reported_measurement_without_limits_never_covers(tmp_path, ratified):
    """A tag without a window grades nothing -- the #3778 rule, unchanged."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a", limits=False), status="reported"
    )
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", **({} if ratified else {"ratified": False})}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], rows)

    assert item["status"] == "unmet"
    assert item["detail"]["uncovered_rows"] == ["row_a"]


def test_item6_passing_measurement_covers_a_ratified_row_as_before(tmp_path):
    """The `status: "pass"` path is untouched: a tagged, limited, passing
    measurement credits its row and the item still grades `met`."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a", status="pass"), status="pass"
    )
    a = _write(tmp_path, "mc_a.json", envelope)

    report, item = _item6_rows(
        tmp_path, [{"file": a, "rows": ["row_a"]}], [{"id": "row_a"}]
    )

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"row_a": "derived"}
    assert report["tier"] == "T1"


def test_item6_refused_citation_credits_no_row(tmp_path):
    """A citation the #2467 campaign-discipline gates refuse keeps its own
    reason and credits nothing, even for an unratified row."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a"),
        status="reported",
    )
    envelope["measurements"][0]["sample_size"] = {
        **envelope["measurements"][0]["sample_size"],
        "verdict": "insufficient",
        "required_n": 18000,
        "required_n_for_target": 20000,
    }
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", "ratified": False}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "undersized_sample"
    # The row is reported against the refusal (`failed_rows`), never
    # credited -- the citation never reached the crediting step at all.
    assert item["detail"]["failed_rows"] == ["row_a"]
    assert item["detail"]["uncovered_rows"] == []
    assert item["detail"]["row_sources"] == {}


def test_item6_reported_and_passing_tags_for_one_row_both_cover(tmp_path):
    """Two tagged measurements for the same row, one `reported` and one
    `pass`: the row is covered either way."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a"),
        _yield_meas_tagged("vref_hot", "row_a", status="pass"),
        status="reported",
    )
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", "ratified": False}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a"]}], rows)

    assert item["detail"]["uncovered_rows"] == []
    assert item["detail"]["row_sources"] == {"row_a": "derived"}


def test_item6_reported_tags_credit_only_the_unratified_row(tmp_path):
    """One citation naming a ratified and an unratified row, both backed by
    `reported` measurements: only the unratified one is credited."""
    envelope = _yield_envelope(
        _yield_meas_tagged("vref", "row_a"),
        _yield_meas_tagged("gain", "row_b"),
        status="reported",
    )
    a = _write(tmp_path, "mc_a.json", envelope)
    rows = [{"id": "row_a", "ratified": False}, {"id": "row_b"}]

    _, item = _item6_rows(tmp_path, [{"file": a, "rows": ["row_a", "row_b"]}], rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["uncovered_rows"] == ["row_b"]
    assert item["detail"]["row_sources"] == {"row_a": "derived"}
