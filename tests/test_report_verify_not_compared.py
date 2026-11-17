"""Issue #5866: a provenance field the rerun does not emit is reported as
`not_compared`, never as drift against `null`.

A committed report may carry provenance keys the fresh run never writes --
spliced in by a downstream wrapper (`gds_sha256`), emitted by a
newer/older klt, or populated by a different verb. "A field this run does
not produce" and "a field whose value changed" are different facts, and
only the second one means the report no longer describes the layout.
"""


def _report(violation_count: int = 0, **provenance: object) -> dict:
    base = {"klt_version": "0.6.0", "klayout_version": "0.29.0"}
    base.update(provenance)
    return {
        "schema_version": 1,
        "status": "clean" if violation_count == 0 else "violations",
        "violation_count": violation_count,
        "rule_counts": {},
        "provenance": base,
    }


def test_provenance_field_absent_from_fresh_is_not_drift():
    """The issue's own reproduction: an extra committed `gds_sha256` alone
    must verify `match` (exit 0), naming the field in `not_compared`."""
    from klayout_tools import _report_verify as rv

    committed = _report(gds_sha256="sha256:" + "210b" * 16)
    fresh = _report()

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["not_compared"] == [
        {"field": "provenance.gds_sha256", "committed": "sha256:" + "210b" * 16}
    ]


def test_provenance_absence_does_not_mask_real_drift():
    """Real drift beside the absent provenance field still fails the
    verification -- `not_compared` never swallows a comparable change."""
    from klayout_tools import _report_verify as rv

    committed = _report(violation_count=0, gds_sha256="sha256:abc")
    fresh = _report(violation_count=3)

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "drifted"
    assert result["not_compared"] == [
        {"field": "provenance.gds_sha256", "committed": "sha256:abc"}
    ]
    assert result["drift"]


def test_nested_provenance_absence_is_also_not_compared():
    """The rule keys off the `provenance` subtree, not one field name -- a
    wrapper-spliced key at any depth under `provenance` qualifies."""
    from klayout_tools import _report_verify as rv

    committed = _report()
    committed["provenance"]["pdk"] = {"version": "a", "wrapper_digest": "d"}
    fresh = _report()
    fresh["provenance"]["pdk"] = {"version": "a"}

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "match"
    assert result["not_compared"] == [
        {"field": "provenance.pdk.wrapper_digest", "committed": "d"}
    ]


def test_verdict_field_absent_from_fresh_still_drifts():
    """Provenance-only rule: a verdict-bearing key (`rule_counts`) missing
    from the fresh report is exactly the regression the diff must catch."""
    from klayout_tools import _report_verify as rv

    committed = _report()
    fresh = _report()
    del fresh["rule_counts"]

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "drifted"
    assert result["drift"] == [{"field": "rule_counts", "committed": {}, "fresh": None}]
    assert "not_compared" not in result


def test_whole_provenance_block_missing_from_fresh_still_drifts():
    """Depth guard: a fresh report that lost its entire `provenance` block
    is a degenerate report, not a set of not-comparable leaves -- the
    whole-block absence stays drift."""
    from klayout_tools import _report_verify as rv

    committed = _report()
    fresh = _report()
    del fresh["provenance"]

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "drifted"
    assert [entry["field"] for entry in result["drift"]] == ["provenance"]


def test_fresh_only_provenance_key_still_drifts():
    """Asymmetric by design: a key the *fresh* report grew (`schema grew`)
    still drifts -- only a committed key the fresh run cannot produce is
    not comparable."""
    from klayout_tools import _report_verify as rv

    committed = _report()
    fresh = _report(new_probe="x")

    result = rv.build_rerun_result(
        report_path="drc.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "drifted"
    assert result["drift"] == [
        {"field": "provenance.new_probe", "committed": None, "fresh": "x"}
    ]


def test_not_compared_omitted_when_empty():
    """No absent provenance fields -> no `not_compared` key: every existing
    verb's payload stays byte-identical."""
    from klayout_tools import _report_verify as rv

    result = rv.build_rerun_result(
        report_path="drc.json", committed=_report(), fresh=_report()
    )

    assert result == {
        "schema_version": 1,
        "mode": "rerun",
        "report": "drc.json",
        "status": "match",
        "drift": [],
        "fresh": _report(),
    }


def test_diff_verdict_fields_drops_provenance_absence_without_collector():
    """The rule lives in `_diff_json`, not the payload assembly: a direct
    `diff_verdict_fields` call sees no drift either, with or without a
    `not_compared` collector to surface the skipped fields."""
    from klayout_tools import _report_verify as rv

    committed = _report(gds_sha256="sha256:abc")
    fresh = _report()

    assert rv.diff_verdict_fields(committed, fresh) == []

    collected: list = []
    assert rv.diff_verdict_fields(committed, fresh, not_compared=collected) == []
    assert collected == [{"field": "provenance.gds_sha256", "committed": "sha256:abc"}]


def test_render_rerun_drift_prints_not_compared_field_names_only(capsys):
    """Text output names the skipped fields so a non-zero exit always means
    "something that could be compared did not match"."""
    from klayout_tools.cli.output import render_rerun_drift

    render_rerun_drift(
        {
            "report": "drc.json",
            "status": "match",
            "drift": [],
            "not_compared": [
                {"field": "provenance.gds_sha256", "committed": "sha256:abc"}
            ],
        }
    )

    assert capsys.readouterr().out == (
        "report: drc.json\n"
        "status: match\n"
        "\n"
        "not compared (provenance fields absent from the fresh report):\n"
        "  provenance.gds_sha256\n"
    )


def test_render_rerun_drift_unchanged_without_not_compared(capsys):
    """Payloads with no `not_compared` print exactly the bytes they did
    before issue #5866."""
    from klayout_tools.cli.output import render_rerun_drift

    render_rerun_drift(
        {
            "report": "drc.json",
            "status": "drifted",
            "drift": [{"field": "violation_count", "committed": 0, "fresh": 2}],
        }
    )

    assert capsys.readouterr().out == (
        "report: drc.json\n"
        "status: drifted\n"
        "\n"
        "drift:\n"
        "  violation_count:\n"
        "      committed: 0\n"
        "      fresh:     2\n"
    )
