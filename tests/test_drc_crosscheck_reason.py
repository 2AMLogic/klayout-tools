"""`comparison.reason` (issue #5720): which not_comparable cause the
curated-vs-native cross-check names, and when the `reason` key is absent.
Split from `tests/test_drc_crosscheck.py` (same fakes: no klayout binary, no
PDK) so both modules stay under the 500-line file-size ratchet."""

from __future__ import annotations

import pytest

import klayout_tools.drc_crosscheck as cc
from klayout_tools.cli import main
from klayout_tools.coverage import build_check_coverage, coverage_rollup, rollup_status
from klayout_tools.drc import CODE_KLAYOUT_UNAVAILABLE, DrcError

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HASH = "sha256:" + "a" * 64
MAPPING = [
    {"id": "m1.width", "curated_rules": ["met1.1"], "native_categories": ["m1.1"]}
]


def _box(left=0, bottom=0, right=10, top=10):
    return {"left": left, "bottom": bottom, "right": right, "top": top}


def _violation(rule, bbox=None):
    return {"rule": rule, "bbox": bbox or _box()}


def _report(
    *,
    engine,
    violations=(),
    checked=(),
    categories=(),
    layout_hash=HASH,
    pdk="sky130A",
    known=True,
):
    """A report with the fields the cross-check reads. ``known=False`` models
    a native run whose coverage is unmeasured (`coverage_unknown`, exit 4)."""
    violations = list(violations)
    unknown = [] if known else [{"id": "engine_execution:klayout", "reason": "x"}]
    coverage = {
        **build_check_coverage(checked=list(checked), unknown=unknown),
        "rules_checked": sorted(checked),
        "rule_categories": sorted(categories),
    }
    report = {"coverage": coverage}
    rollup = coverage_rollup(report, failed=bool(violations))
    status = rollup_status(rollup, success="clean", failure="violations")
    return {
        "schema_version": 2,
        "file": "x.gds",
        "deck": "d",
        **({"engine": "klayout"} if engine == "native" else {}),
        "status": status,
        "violation_count": len(violations),
        "violations": violations,
        "coverage": coverage,
        "provenance": {
            "klt_version": "1",
            "klayout_version": "0.30",
            "pdk": {"name": pdk, "source": "s", "version": "v"},
            "deck": {"name": engine, "content_hash": HASH},
            "input": {"content_hash": layout_hash, "role": "layout"},
        },
    }


def _run(monkeypatch, curated, native, mapping=MAPPING, deck="sky130"):
    monkeypatch.setattr(cc, "run_drc", lambda *a, **k: curated)
    if isinstance(native, Exception):

        def boom(*a, **k):
            raise native

        monkeypatch.setattr(cc, "run_drc_klayout_engine", boom)
    else:
        monkeypatch.setattr(cc, "run_drc_klayout_engine", lambda *a, **k: native)
    return cc.run_drc_crosscheck("x.gds", deck, "native.drc", rule_mapping=mapping)


def _covered(**kw):
    return dict(checked=["met1.1"]), dict(categories=["m1.1"], checked=["m1.1"], **kw)


def test_shipped_gf180mcu_mapping_is_the_one_verified_group():
    """AC1 (issue #5720): exactly one shipped group, the verified
    ``comp.enclosing.contact.1`` ↔ ``CO.4`` equivalence; ``comp.width.mv.1``
    ↔ ``DF.1a_MV`` stays unmapped (the ``mvsd``/``mvpsd`` exclusion)."""
    assert set(cc.RULE_MAPPING) == {"gf180mcu"}
    (group,) = cc.RULE_MAPPING["gf180mcu"]
    assert group["curated_rules"] == ["comp.enclosing.contact.1"]
    assert group["native_categories"] == ["CO.4"]
    assert all(
        "comp.width.mv.1" not in g["curated_rules"]
        for g in cc.RULE_MAPPING.get("gf180mcu", [])
    )


def test_reason_mapping_empty(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch,
        _report(engine="curated", **c_kw),
        _report(engine="native", **n_kw),
        mapping=[],
    )
    assert out["comparison"]["state"] == "not_comparable"
    assert out["comparison"]["reason"] == cc.REASON_MAPPING_EMPTY


def test_reason_group_not_covered(monkeypatch):
    # The native deck never declared the mapped category: no comparison.
    curated = _report(engine="curated", checked=["met1.1"])
    native = _report(engine="native", categories=[], checked=[])
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["state"] == "not_comparable"
    assert out["comparison"]["reason"] == cc.REASON_GROUP_NOT_COVERED


def test_reason_group_not_covered_precedes_unmapped_rules_ran(monkeypatch):
    # Both causes present: the group-level one is more fundamental.
    curated = _report(engine="curated", checked=["met1.1", "met2.1"])
    native = _report(engine="native", categories=[], checked=[])
    out = _run(monkeypatch, curated, native)
    assert out["comparison"]["reason"] == cc.REASON_GROUP_NOT_COVERED


def test_gf180mcu_group_is_reported_but_state_stays_not_comparable(monkeypatch):
    """AC4 (issue #5720): the shipped gf180mcu group makes ``groups``
    non-empty and its own verdict agree_violations, while unmapped rules on
    both sides keep the overall state ``not_comparable`` (never
    ``agree_violations``), reasoned ``unmapped_rules_ran`` — the live run's
    shape."""
    curated = _report(
        engine="curated",
        violations=[_violation("comp.enclosing.contact.1")],
        checked=["comp.enclosing.contact.1", "comp.width.1"],
    )
    native = _report(
        engine="native",
        violations=[_violation("CO.4", _box(0, 0, 6, 10))],
        categories=["CO.4", "DF.1a_MV"],
        checked=["CO.4"],
    )
    # mapping=None: the shipped RULE_MAPPING for gf180mcu is used.
    out = _run(monkeypatch, curated, native, mapping=None, deck="gf180mcu")
    comparison = out["comparison"]
    assert comparison["state"] == "not_comparable"
    assert comparison["reason"] == cc.REASON_UNMAPPED_RULES_RAN
    assert [g["id"] for g in comparison["groups"]] == ["contact.enclosure"]
    assert comparison["groups"][0]["state"] == "agree_violations"
    assert comparison["unmapped"]["curated"] == ["comp.width.1"]
    assert comparison["unmapped"]["native"] == ["DF.1a_MV"]


def test_reason_absent_when_agree_clean(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch, _report(engine="curated", **c_kw), _report(engine="native", **n_kw)
    )
    assert out["comparison"]["state"] == "agree_clean"
    assert "reason" not in out["comparison"]


def test_reason_absent_when_disagreement(monkeypatch):
    c_kw, n_kw = _covered()
    out = _run(
        monkeypatch,
        _report(engine="curated", violations=[_violation("met1.1")], **c_kw),
        _report(engine="native", **n_kw),
    )
    assert out["comparison"]["state"] == "disagreement"
    assert "reason" not in out["comparison"]


@pytest.mark.parametrize(
    "exc",
    [
        DrcError("no klayout", code=CODE_KLAYOUT_UNAVAILABLE),
        DrcError("deck exploded"),
    ],
)
def test_reason_absent_on_native_side_failures(monkeypatch, exc):
    curated = _report(engine="curated", checked=["met1.1"])
    out = _run(monkeypatch, curated, exc)
    assert out["comparison"]["state"] in ("native_unavailable", "engine_failed")
    assert "reason" not in out["comparison"]


def test_reason_absent_when_missing_native_deck(monkeypatch):
    monkeypatch.setattr(
        cc, "run_drc", lambda *a, **k: _report(engine="curated", checked=["a"])
    )
    out = cc.run_drc_crosscheck("x.gds", "sky130", None)
    assert out["comparison"]["state"] == "native_unavailable"
    assert "reason" not in out["comparison"]


def test_cli_engine_both_text_prints_reason(monkeypatch, capsys):
    """AC8 (issue #5720): `--format text` prints the reason next to the
    comparison state."""
    curated = _report(
        engine="curated", checked=["comp.enclosing.contact.1", "comp.width.1"]
    )
    native = _report(engine="native", categories=["CO.4"], checked=["CO.4"])
    out = _run(monkeypatch, curated, native, mapping=None, deck="gf180mcu")
    monkeypatch.setattr(
        "klayout_tools.cli.drc_cmd.run_drc_crosscheck", lambda *a, **k: out
    )
    monkeypatch.setattr(
        "klayout_tools.cli.drc_cmd.pdk_module.drc_deck_file", lambda **k: "native.drc"
    )
    rc = main(["drc", "x.gds", "--engine", "both", "--deck", "gf180mcu"])
    text = capsys.readouterr().out
    assert rc == 0
    assert "comparison: not_comparable" in text
    assert "reason: unmapped_rules_ran" in text
